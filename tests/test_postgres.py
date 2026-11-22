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
    from pantree_insights.graph import CONFIRM_QUESTION, CONFIRM_YES, Agent
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
        final = list(second.resume(run_id, {CONFIRM_QUESTION: CONFIRM_YES}))[-1]
        assert final["status"] == "done" and final["grounded"] and final["trust"] == "verified"
        assert second._checkpoint_context.get_stats()["pool_size"] <= 4
    finally:
        second.close()
