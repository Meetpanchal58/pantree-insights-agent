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
