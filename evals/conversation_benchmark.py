"""Local blind-confirmation benchmark; no model, network, or stakeholder wait in timings.

Run against either baseline or candidate via PYTHONPATH; shared independent SQL is in fastpath_baseline.
"""
from __future__ import annotations

import hashlib
import json
import platform
import tempfile
import time
from pathlib import Path

import pandas as pd
from fastpath_baseline import CASES

from pantree_insights.config import ROOT
from pantree_insights.db import manifest
from pantree_insights.graph import Agent
from pantree_insights.llm import FakeLLM
from pantree_insights.memory import Memory


def distribution(values):
    return {"n": len(values), "p50": float(pd.Series(values).quantile(.5)),
            "p95": float(pd.Series(values).quantile(.95))} if values else None


def forbidden(*_):
    raise AssertionError('This benchmark must not invoke a model')


def main():
    startups, rows, revisions = [], [], []
    for _ in range(3):
        started = time.perf_counter()
        agent = Agent(llm_factory=lambda: FakeLLM(forbidden))
        startups.append(time.perf_counter() - started)
        agent.close()
    agent = Agent(llm_factory=lambda: FakeLLM(forbidden))
    with tempfile.TemporaryDirectory() as folder:
        for repeat in range(5):
            for i, (question, reference) in enumerate(CASES):
                started = time.perf_counter()
                run_id, stream = agent.start(question, memory=Memory(Path(folder) / f'{repeat}-{i}', 'small'))
                events = list(stream)
                interpretation = time.perf_counter() - started
                pending = events[-1]
                confirmation = pending.get('kind') == 'confirm'
                execution = None
                if confirmation:
                    assert not any(e['type'] == 'result' for e in events)
                    started = time.perf_counter()
                    events += list(agent.resume(run_id, 'Go ahead', pending['plan_version']))
                    execution = time.perf_counter() - started
                final = events[-1]
                assert final['status'] == 'done' and final['grounded'] and final['cost']['calls'] == 0
                actual = pd.DataFrame(final['result']['data'], columns=final['result']['columns'])
                expected = agent.con.execute(reference.format(last=agent.data['last_day'])).df()
                for col in expected:
                    if pd.api.types.is_datetime64_any_dtype(expected[col]):
                        actual[col] = pd.to_datetime(actual[col])
                pd.testing.assert_frame_equal(actual, expected, check_dtype=False, rtol=1e-10, atol=1e-8)
                rows.append({'question': question, 'repeat': repeat, 'correct': True, 'confirmation': confirmation,
                             'interpretation': interpretation if confirmation else None,
                             'post_confirmation': execution, 'total': interpretation + (execution or 0),
                             'model_calls': final['cost']['calls'], 'tokens': final['cost']['input'] +
                             final['cost']['output'], 'usd': final['cost']['usd'],
                             'stages': {e['step']: e['detail'].get('seconds') for e in events
                                        if e['type'] == 'step' and e['status'] == 'done'}})
            if confirmation:
                run_id, stream = agent.start('Show revenue by category',
                    memory=Memory(Path(folder) / f'revision-{repeat}', 'small'))
                proposal = list(stream)[-1]
                started = time.perf_counter()
                revised = list(agent.resume(run_id, 'No, by brand instead', proposal['plan_version']))[-1]
                revisions.append(time.perf_counter() - started)
                assert revised['spec']['dimensions'] == ['brand'] and revised['kind'] == 'confirm'
                list(agent.resume(run_id, 'Cancel', revised['plan_version']))
    agent.close()
    sources = sorted((ROOT / 'src').rglob('*.py')) + sorted((ROOT / 'semantic').glob('*.yaml'))
    sources += sorted((ROOT / 'prompts').glob('*.yaml'))
    source_hash = hashlib.sha256(b''.join(p.relative_to(ROOT).as_posix().encode() + p.read_bytes()
                                       for p in sources)).hexdigest()
    print(json.dumps({'source_sha256': source_hash, 'kind': 'local_no_model', 
        'python': platform.python_version(), 'data': manifest(),
        'cold_definition': 'fresh Agent; Python imports and OS caches may be warm',
        'agent_initialization': distribution(startups), 'first_pass': distribution([r['total'] for r in rows
        if r['repeat'] == 0]), 'warm_total': distribution([r['total'] for r in rows if r['repeat'] > 0]),
        'interpretation': distribution([r['interpretation'] for r in rows if r['interpretation'] is not None]),
        'post_confirmation': distribution([r['post_confirmation'] for r in rows
                                          if r['post_confirmation'] is not None]),
        'revision': distribution(revisions), 'rows': rows, 'cloud_run': None, 'gcs': None,
        'live_model_accuracy': None}, indent=2))


if __name__ == '__main__':
    main()
