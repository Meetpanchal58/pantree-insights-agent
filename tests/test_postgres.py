"""Real PostgreSQL integration; isolated database, enabled by PANTREE_TEST_POSTGRES_DSN."""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from pantree_insights.store import Store


@pytest.fixture(scope="module")
def postgres():
    dsn = os.getenv("PANTREE_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("PANTREE_TEST_POSTGRES_DSN is required for real PostgreSQL integration")
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    name = "pantree_test_" + uuid.uuid4().hex[:12]
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        params = conninfo_to_dict(dsn)
        params["dbname"] = name
        isolated = make_conninfo(**params)
        # The application accepts URL-form PostgreSQL DSNs.
        from sqlalchemy import URL
        url = URL.create("postgresql+psycopg", username=params.get("user", "postgres"),
                         password=params.get("password"), host=params.get("host", "localhost"),
                         port=int(params.get("port", 5432)), database=name).render_as_string(hide_password=False)
        store = Store(url)
        try:
            yield store, url, isolated
        finally:
            store.engine.dispose()
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def test_postgres_quota_and_spend_are_atomic(postgres):
    store, _, _ = postgres
    user = store.upsert_user("quota@example.test")
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: store.reserve_question(user["id"], 7), range(40)))
    assert sum(results) == store.questions_today(user["id"]) == 7
    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(lambda _: store.add_usd_today(user["id"], 0.01), range(40)))
    assert store.usd_today(user["id"]) == pytest.approx(0.4)


def test_postgres_terminal_retries_commit_once(postgres):
    store, _, _ = postgres
    user = store.upsert_user("retry@example.test")
    chat = store.create_chat(user["id"])
    store.create_run("retry", user["id"], chat["id"], "GMV", use_defaults=True)
    final = {"status": "done", "cost": {"usd": 0.25}, "run_id": "retry"}
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: store.complete_run("retry", final, [final], {}, "answer", final), range(30)))
    assert sum(results) == 1
    assert len(store.messages(chat["id"])) == 1 and store.usd_today(user["id"]) == 0.25
    assert store.run_record(user["id"], "retry")["summary"]["use_defaults"] is True


def test_postgres_terminal_failure_rolls_back_all_writes(postgres):
    store, _, _ = postgres
    user = store.upsert_user("rollback@example.test")
    chat = store.create_chat(user["id"])
    store.create_run("rollback", user["id"], chat["id"], "GMV")
    # PostgreSQL rejects a NUL in text after updating the run in the same transaction.
    with pytest.raises(Exception, match="NUL|0x00|zero|character"):
        store.complete_run("rollback", {"status": "done", "cost": {"usd": 1}}, [], {}, "bad\x00text", {})
    assert store.run_record(user["id"], "rollback")["status"] == "running"
    assert store.messages(chat["id"]) == [] and store.usd_today(user["id"]) == 0
    assert store.complete_run("rollback", {"status": "done"}, [], {}, "good", {})


def test_postgres_checkpoint_survives_agent_restart(postgres, con, tmp_path, monkeypatch):
    from pantree_insights.config import get_settings
    from pantree_insights.graph import Agent
    from pantree_insights.llm import FakeLLM
    from pantree_insights.memory import Memory
    _, url, _ = postgres
    settings = get_settings()
    monkeypatch.setattr(settings, "database_url", url)
    monkeypatch.setattr(settings, "checkpoint_backend", "postgres")
    def response(role, *_):
        return {"readings": []} if role == "contrast" else {
            "restated_question": "GMV across all data", "route": "spec", "spec": {
                "table": "sales", "measures": [{"name": "gmv", "metric": "gross_sales"}]}}
    memory = Memory(tmp_path, "small")
    first = Agent(llm_factory=lambda: FakeLLM(response), con=con, memory=memory)
    run_id, stream = first.start("How much did we sell?")
    events = list(stream)
    assert events[-1]["type"] == "clarify" and events[-1]["kind"] == "confirm"
    first.close()
    second = Agent(llm_factory=lambda: FakeLLM(response), con=con, memory=memory)
    try:
        second.restore_memory(run_id, memory)
        final = list(second.resume(run_id, "Go ahead", 1))[-1]
        assert final["status"] == "done" and final["grounded"] and final["trust"] == "verified"
        assert second._checkpoint_context.get_stats()["pool_size"] <= 4
    finally:
        second.close()


def test_concurrent_replies_are_persisted_once(postgres):
    store, _, _ = postgres
    user = store.upsert_user('reply@example.test')
    chat = store.create_chat(user['id'])
    store.create_run('reply-once', user['id'], chat['id'], 'Orders')
    proposal = {'type': 'clarify', 'plan_version': 1, 'message': 'Count orders. Go ahead?'}
    store.save_pause('reply-once', chat['id'], [proposal], proposal)
    def reply(_):
        try:
            store.save_reply('reply-once', chat['id'], [proposal, {'type': 'answered'}], 'Go ahead')
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reply, range(20))) == 1
    assert [m['role'] for m in store.messages(chat['id'])] == ['assistant', 'user']


def test_investigation_survives_store_restart_and_history_window(postgres):
    store, url, _ = postgres
    user = store.upsert_user('context@example.test')
    outsider = store.upsert_user('outsider@example.test')
    chat = store.create_chat(user['id'])
    for i in range(12):
        run_id = f'context-{i}'
        store.create_run(run_id, user['id'], chat['id'], f'Question {i}')
        final = {'status': 'done', 'grounded': True, 'question': f'Question {i}', 'objective': 'Find volume drivers',
                 'relationship': 'new' if i == 0 else 'drilldown',
                 'confirmed_plan': [{'spec': {'metric': f'metric-{i}'}, 'sql': 'SELECT 1'}],
                 'metric_definitions': {'orders': {'description': 'Distinct placed orders'}},
                 'analysis': {'follow_up_questions': ['Investigate mix']}}
        assert store.complete_run(run_id, final, [], {}, f'Finding {i}', final)
    restarted = Store(url)
    try:
        context = restarted.investigation(user['id'], chat['id'])
        assert context['objective'] == 'Find volume drivers'
        assert context['origin_plan'][0]['spec']['metric'] == 'metric-0'
        assert context['current_plan'][0]['spec']['metric'] == 'metric-11'
        assert len(context['evidence']) == 8 and 'Finding 11' in str(context)
        assert context['metric_definitions']['orders']['description'] == 'Distinct placed orders'
        assert 'sql' not in context['current_plan'][0]
        assert restarted.investigation(outsider['id'], chat['id']) == {}
        assert 'Question 0' not in restarted.conversation(chat['id'])[0]
        restarted.delete_chat(user['id'], chat['id'])
        assert restarted.investigation(user['id'], chat['id']) == {}
    finally:
        restarted.engine.dispose()


def test_postgres_shared_model_budget_and_pinned_credentials_survive_restart(postgres, monkeypatch):
    from cryptography.fernet import Fernet
    from pydantic import SecretStr

    from pantree_insights.config import get_settings
    from pantree_insights.llm import BUDGET_KEY, BudgetExceeded, ChatLLM
    from pantree_insights.providers import ProviderError
    store, url, _ = postgres
    monkeypatch.setenv('OPENAI_API_KEY', 'shared-postgres-test-key')
    monkeypatch.setattr(get_settings(), 'credential_encryption_key', SecretStr(Fernet.generate_key().decode()))
    initial = store.usd_today(BUDGET_KEY)
    monkeypatch.setattr(get_settings(), 'daily_budget_usd', initial + 1)
    monkeypatch.setattr(get_settings(), 'daily_user_budget_usd', .75)
    people = [store.upsert_user(f'budget-{i}@example.test') for i in range(2)]
    pins = [store.pin_ai(u) for u in people]
    def reserve(i):
        try:
            return store.reserve_api_call({**pins[i % 2], 'model': f'model-{i % 3}'}, 'pg-budget', 'analyst', .25)
        except BudgetExceeded:
            return None
    with ThreadPoolExecutor(max_workers=12) as pool:
        accepted = [cid for cid in pool.map(reserve, range(36)) if cid]
    assert len(accepted) == 4
    assert store.usd_today(BUDGET_KEY) == pytest.approx(initial + 1)
    assert all(store.usd_today(u['id']) <= .75 for u in people)
    user = people[0]
    store.add_credential(user['id'], 'groq', 'personal-postgres-test-key', 'llama-3.3-70b-versatile')
    pin = store.pin_ai(user)
    chat = store.create_chat(user['id'])
    store.create_run('pg-pin', user['id'], chat['id'], 'Orders', ai=pin)
    proposal = {'type': 'clarify', 'plan_version': 1}
    store.save_pause('pg-pin', chat['id'], [proposal], proposal)
    cid = store.reserve_api_call(pin, 'pg-pin', 'interpreter', .5)
    store.settle_api_call(cid, .02, {'input': 100, 'cached': 20, 'output': 5})
    store.set_ai_profile(user['id'], model='next-model')
    restarted = Store(url)
    try:
        saved = restarted.run_record(user['id'], 'pg-pin')['summary']['ai']
        assert saved == pin and restarted.ai_key(saved) == 'personal-postgres-test-key'
        assert ChatLLM(selection=saved, store=restarted, run_id='pg-pin').tracker.usd == .02
        restarted.remove_credentials(user['id'])
        with pytest.raises(ProviderError, match='revoked'):
            restarted.ai_key(saved)
    finally:
        restarted.engine.dispose()


def test_existing_postgres_usage_column_migrates_without_losing_spend(postgres):
    from sqlalchemy import inspect, text
    store, url, _ = postgres
    # Simulate the old schema after removing only this test database's synthetic budget row.
    with store.engine.begin() as connection:
        connection.execute(text('DELETE FROM usage WHERE length(user_id) > 16'))
        connection.execute(text('ALTER TABLE usage ALTER COLUMN user_id TYPE VARCHAR(16)'))
    user = store.upsert_user('migration@example.test')
    store.add_usd_today(user['id'], .35)
    migrated = Store(url)
    try:
        assert next(c for c in inspect(migrated.engine).get_columns('usage')
                    if c['name'] == 'user_id')['type'].length == 64
        assert migrated.usd_today(user['id']) == .35
    finally:
        migrated.engine.dispose()


def test_web_runner_restores_original_model_after_settings_change(postgres, con, monkeypatch):
    from test_web import responder

    from pantree_insights import providers
    from pantree_insights.config import get_settings
    from pantree_insights.db import snapshot_signature
    from pantree_insights.graph import Agent
    from pantree_insights.web import Runner
    store, url, _ = postgres
    monkeypatch.setattr(get_settings(), 'database_url', url)
    monkeypatch.setattr(get_settings(), 'checkpoint_backend', 'postgres')
    monkeypatch.setattr(get_settings(), 'daily_budget_usd', 10)
    monkeypatch.setenv('OPENAI_API_KEY', 'original-shared-test-key')
    user = store.upsert_user('runner-pin@example.test')
    chat = store.create_chat(user['id'])
    original_model = store.pin_ai(user)['model']
    monkeypatch.setattr(providers, 'discover', lambda *a, **kw: [{'id': original_model}])
    calls = []
    def invoke(provider, key, model, schema, system, prompt):
        calls.append((provider, key, model))
        role = ('interpreter' if 'restated_question' in schema.model_fields else
                'contrast' if 'readings' in schema.model_fields else 'analyst')
        return responder(role, schema, system, prompt), {'input': 100, 'cached': 20, 'output': 30, 'reported': True}
    monkeypatch.setattr(providers, 'invoke', invoke)
    def make_runner():
        runner = Runner()
        runner._agent = Agent(con=con)
        runner._snapshot = snapshot_signature()
        return runner
    first = make_runner()
    run = first.start(store, user, chat, 'How much did we sell?')
    assert run.segment_done.wait(30) and run.paused
    prior_calls = len(calls)
    first.agent.close()
    store.set_ai_profile(user['id'], model='future-model')
    second = make_runner()
    try:
        restored = second.restore(store, user, run.id)
        assert restored.ai['model'] == original_model
        assert len(second.agent._llms[run.id].tracker.calls) == prior_calls
        second.answer(store, restored, 'Go ahead', restored.events[-1]['plan_version'])
        assert restored.segment_done.wait(30)
        assert restored.events[-1]['status'] == 'done'
        assert len(calls) > prior_calls
        assert set(calls) == {('openai', 'original-shared-test-key', original_model)}
    finally:
        second.agent.close()


def test_concurrent_credential_replacements_leave_only_current_version_active(postgres, monkeypatch):
    from cryptography.fernet import Fernet
    from pydantic import SecretStr

    from pantree_insights.config import get_settings
    store, _, _ = postgres
    monkeypatch.setattr(get_settings(), 'credential_encryption_key', SecretStr(Fernet.generate_key().decode()))
    user = store.upsert_user('key-race@example.test')
    def replace(i):
        return store.add_credential(user['id'], 'groq', f'concurrent-test-key-{i}', 'llama-3.3-70b-versatile')
    with ThreadPoolExecutor(max_workers=8) as pool:
        versions = list(pool.map(replace, range(16)))
    active = [cid for cid in versions if store.credential(user['id'], cid)]
    assert active == [store.ai_profile(user['id'])['credential_id']]


def test_adaptive_probe_and_evidence_survive_postgres_restart(postgres, con, tmp_path, monkeypatch):
    from test_adaptive_investigations import configure, probe, save, start

    from pantree_insights.config import get_settings
    from pantree_insights.graph import Agent
    from pantree_insights.memory import Memory

    store, url, _ = postgres
    monkeypatch.setattr(get_settings(), 'database_url', url)
    monkeypatch.setattr(get_settings(), 'checkpoint_backend', 'postgres')
    memory = Memory(tmp_path / 'adaptive', 'small')
    first = Agent(con=con, memory=memory)
    configure(first, [probe()])
    rid, initial = start(first)
    next_step = list(first.resume(rid, 'go ahead', initial['plan_version']))[-1]
    assert next_step['kind'] == 'confirm'
    first.close()
    restored = Agent(con=con, memory=memory)
    configure(restored, [{'action': 'stop'}])
    try:
        restored.restore_memory(rid, memory)
        final = list(restored.resume(rid, 'go ahead', next_step['plan_version']))[-1]
        assert final['status'] == 'done' and len(final['analyses']) == 2 and final['probe_ledger']
        user = store.upsert_user('recall-pg@example.test')
        chat = store.create_chat(user['id'])
        save(store, user, chat, 'abcdefabcdef', 'Footwear refund investigation')
        recalled = store.recall(user['id'], chat['id'], 'earlier footwear refund investigation', 'snapshot-a')
        assert recalled['retrieved_evidence'][0]['run_id'] == 'abcdefabcdef'
    finally:
        restored.close()
