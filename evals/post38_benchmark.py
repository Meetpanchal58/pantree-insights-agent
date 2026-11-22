"""Local, no-model measurements. Select baseline sources with PYTHONPATH; never infers cloud latency.

DATA_SIZE=small python evals/post38_benchmark.py > report.json
Optional PANTREE_TEST_POSTGRES_DSN benchmarks an isolated temporary database and drops it afterwards.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import tempfile
import time
import uuid
from pathlib import Path

import duckdb
import pandas as pd
from fastpath_baseline import CASES
from provenance import latency

from pantree_insights.config import ROOT, get_settings
from pantree_insights.db import connect, manifest, snapshot_signature
from pantree_insights.graph import Agent
from pantree_insights.llm import FakeLLM
from pantree_insights.memory import Memory


def no_model(*_):
    raise RuntimeError("model required; intentionally unavailable in this routing benchmark")


def timed(fn, n=20):
    samples = []
    for _ in range(n):
        start = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - start)
    return latency(samples)


def completed_without_model(stream):
    try:
        return list(stream)[-1]['status'] == 'done'
    except RuntimeError as error:
        if str(error).startswith('model required;'):
            return False
        raise


def main():
    report = {"kind": "local_no_model", "python": platform.python_version(), "data": manifest(),
              "definition_signature": snapshot_signature(), "source_root": ROOT.name,
              "cold_definition": "fresh Agent; imports, published joins and OS caches may be warm",
              "cloud_run": None, "gcs_mount": None, "live_model_accuracy": None, "model_cost_usd": 0}
    paths = sorted((ROOT / 'src').rglob('*.py')) + sorted((ROOT / 'semantic').glob('*.yaml'))
    report['source_sha256'] = hashlib.sha256(b''.join(p.relative_to(ROOT).as_posix().encode() + p.read_bytes()
                                                    for p in paths)).hexdigest()
    startup = []
    for _ in range(3):
        start = time.perf_counter()
        agent = Agent(llm_factory=lambda: FakeLLM(no_model))
        startup.append(time.perf_counter() - start)
        agent.close()
    report['agent_preparation'] = latency(startup)
    agent = Agent(llm_factory=lambda: FakeLLM(no_model))
    rows, routes = [], []
    with tempfile.TemporaryDirectory() as tmp:
        for repeat in range(5):
            for i, (question, reference) in enumerate(CASES):
                start = time.perf_counter()
                _, stream = agent.start(question, memory=Memory(Path(tmp) / f'{repeat}-{i}', 'small'))
                events = list(stream)
                elapsed = time.perf_counter() - start
                final = events[-1]
                assert final['status'] == 'done' and final['grounded'] and final['cost']['calls'] == 0
                actual = pd.DataFrame(final['result']['data'], columns=final['result']['columns'])
                expected = agent.con.execute(reference.format(last=agent.data['last_day'])).df()
                for col in expected:
                    if pd.api.types.is_datetime64_any_dtype(expected[col]):
                        actual[col] = pd.to_datetime(actual[col])
                pd.testing.assert_frame_equal(actual, expected, check_dtype=False, rtol=1e-10, atol=1e-8)
                rows.append({'question': question, 'repeat': repeat, 'seconds': elapsed, 'correct': True,
                             'model_calls': 0, 'stage_seconds': {e['step']: e.get('detail', {}).get('seconds')
                             for e in events if e['type'] == 'step' and e['status'] == 'done'}})
        history = 'User: Show orders\nAnalyst: A prior answer'
        for i, (question, _) in enumerate(CASES):
            memory = Memory(Path(tmp) / f'conversation-{i}', 'small')
            _, stream = agent.start(question, history=history, memory=memory)
            routes.append({'question': question, 'completed_without_model': completed_without_model(stream)})
        memory = Memory(Path(tmp) / 'repeated', 'small')
        question = CASES[0][0]
        for repeat in range(2):
            _, stream = agent.start(question, memory=memory)
            routes.append({'question': question, 'shared_memory_repeat': repeat,
                           'completed_without_model': completed_without_model(stream)})
    report.update(rows=rows, routes=routes, first_pass=latency([r['seconds'] for r in rows if r['repeat'] == 0]),
                  warm=latency([r['seconds'] for r in rows if r['repeat'] > 0]))
    agent.close()
    # Identical independent query, 20 repetitions. Materialization is experimental, not enabled in the app.
    parquet = connect()
    materialized = duckdb.connect()
    started = time.perf_counter()
    table = parquet.execute('SELECT placed_at, line_type, gross_amount FROM sales').to_arrow_table()
    materialized.register('source_rows', table)
    materialized.execute('CREATE TABLE sales AS SELECT * FROM source_rows')
    materialized.unregister('source_rows')
    setup = time.perf_counter() - started
    sql = "SELECT date_trunc('month',placed_at), SUM(gross_amount) FROM sales WHERE line_type='Sale' GROUP BY 1"
    expected = sorted(parquet.execute(sql).fetchall())
    assert sorted(materialized.execute(sql).fetchall()) == expected
    report['storage'] = {'materialization_seconds': setup, 'rows': len(table),
                         'local_parquet': timed(lambda: parquet.execute(sql).fetchall()),
                         'materialized_duckdb': timed(lambda: materialized.execute(sql).fetchall()),
                         'scope': 'sales fact, three columns; OS caches uncontrolled; excludes agent result cache'}
    parquet.close()
    materialized.close()
    if os.getenv('PANTREE_TEST_POSTGRES_DSN'):
        report['postgres'] = postgres_benchmark()
    print(json.dumps(report, indent=2, default=str))


def postgres_benchmark():
    import psycopg
    from langgraph.checkpoint.base import empty_checkpoint
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict
    from sqlalchemy import URL, insert

    from pantree_insights.store import Store, messages
    dsn = os.environ['PANTREE_TEST_POSTGRES_DSN']
    name = 'pantree_bench_' + uuid.uuid4().hex[:10]
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        params = conninfo_to_dict(dsn)
        url = URL.create('postgresql+psycopg', username=params.get('user', 'postgres'),
                         password=params.get('password'), host=params.get('host', 'localhost'),
                         port=int(params.get('port', 5432)), database=name).render_as_string(hide_password=False)
        settings = get_settings()
        old = settings.database_url, settings.checkpoint_backend
        store, agent = None, None
        try:
            store = Store(url)
            user = store.upsert_user('benchmark@example.test')
            chat = store.create_chat(user['id'])
            with store.engine.begin() as c:
                c.execute(insert(messages), [{'id': uuid.uuid4().hex[:16], 'chat_id': chat['id'], 'role': 'user',
                          'content': 'Show orders ' + 'x' * 500} for _ in range(2000)])
            result = {'history_2000_messages': timed(lambda: store.history(chat['id']))}
            settings.database_url, settings.checkpoint_backend = url, 'postgres'
            start = time.perf_counter()
            agent = Agent(llm_factory=lambda: FakeLLM(no_model))
            result['agent_preparation_seconds'] = time.perf_counter() - start
            config = {'configurable': {'thread_id': 'bench', 'checkpoint_ns': ''}}
            checkpoint = empty_checkpoint()
            result['checkpoint_write'] = timed(lambda: agent._saver.put(
                config, checkpoint, {'source': 'input', 'step': 0, 'parents': {}}, {}))
            result['checkpoint_read'] = timed(lambda: agent._saver.get_tuple(config))
            return result
        finally:
            settings.database_url, settings.checkpoint_backend = old
            if agent:
                agent.close()
            if store:
                store.engine.dispose()
            admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))


if __name__ == '__main__':
    main()
