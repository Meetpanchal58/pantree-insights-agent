"""The web app end to end (sign-in, chats, projects, live runs, memory, demos) with a fake LLM."""
import json
import shutil

import pytest

from pantree_insights.config import ROOT

pytestmark = pytest.mark.data

SPEC = {"route": "spec", "restated_question": "Realized revenue by category",
        "spec": {"table": "sales", "measures": [{"name": "realized_revenue", "metric": "realized_revenue"}],
                 "dimensions": ["category"]}}
CLARIFY = {"route": "clarify", "restated_question": "Sales", "questions": [
    {"question": "Which measure?", "options": ["Realized revenue", "Orders"], "default": "Realized revenue",
     "why": "changes the ranking"}]}
PROMPTS = []


def analysis(user):
    lines = user.split("## Result table\n", 1)[1].splitlines()
    header, row = lines[0].split(","), lines[1].split(",")
    claim = {"id": "c1", "value": float(row[1]), "unit": "inr", "column": header[1],
             "row": [{"column": header[0], "value": row[0]}], "op": "value"}
    return {"headline": f"{row[0]} leads with Rs {{c1}}.", "findings": [f"{row[0]}: Rs {{c1}}"],
            "memory": [{"statement": f"{row[0]} is the top category", "type": "fact"}],
            "chart_metric": "realized_revenue", "claims": [claim]}


def responder(agent, schema, system, user):
    PROMPTS.append((agent, user))
    if agent == "interpreter":
        if "clarify me" in user and "answers" not in user:
            return CLARIFY
        if "Which measure?: Orders" in user:
            return {"route": "spec", "restated_question": "Orders", "spec": {
                "table": "sales", "measures": [{"name": "orders", "metric": "orders"}]}}
        return SPEC
    if agent == "contrast":
        return {"readings": []}
    return analysis(user)


@pytest.fixture()
def app_env(tmp_path, monkeypatch, shop_dir, con):
    from pantree_insights import auth, config, store, web
    from pantree_insights.llm import FakeLLM

    memory_dir = tmp_path / "memory"
    (memory_dir / "kpis").mkdir(parents=True)
    shutil.copy(ROOT / "memory" / "kpis" / "kpi_definitions.yaml", memory_dir / "kpis")
    monkeypatch.setenv("DATA_SIZE", "small")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'app.db'}")
    monkeypatch.setenv("MEMORY_DIR", str(memory_dir))
    monkeypatch.setenv("DEMO_DIR", str(tmp_path / "demos"))
    monkeypatch.setenv("ADMIN_EMAILS", '["boss@example.com"]')
    monkeypatch.setenv("DAILY_QUESTION_LIMIT", "3")
    config.get_settings.cache_clear()
    store.get_store.cache_clear()

    codes = {}
    monkeypatch.setattr(auth, "send_code", lambda email, code: codes.__setitem__(email, code))

    from pantree_insights.graph import Agent
    web.runner = web.Runner(lambda: Agent(llm_factory=lambda: FakeLLM(responder), con=con))
    monkeypatch.setattr(web, "lifespan", None, raising=False)
    from fastapi.testclient import TestClient
    app = web.create_app()
    app.router.lifespan_context = _no_lifespan
    PROMPTS.clear()
    yield TestClient(app), codes, tmp_path
    config.get_settings.cache_clear()
    store.get_store.cache_clear()


from contextlib import asynccontextmanager  # noqa: E402


@asynccontextmanager
async def _no_lifespan(app):
    yield


def sign_in(client, codes, email="ana@example.com"):
    assert client.post("/auth/email/request", json={"email": email}).status_code == 200
    assert client.post("/auth/email/verify", json={"email": email, "code": codes[email]}).status_code == 200


def run_to_end(client, run_id, since=0, confirm=True):
    """Stream a run; read-backs are confirmed automatically (other clarifying questions are returned)."""

    events = []
    while True:
        with client.stream("GET", f"/api/runs/{run_id}/stream?since={since + len(events)}") as r:
            for line in r.iter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
        last = events[-1] if events else {}
        if not (confirm and last.get("type") == "clarify" and last.get("kind") == "confirm"):
            return events
        answer = client.post(f"/api/runs/{run_id}/answer",
                             json={"message": "Go ahead", "plan_version": events[-1]["plan_version"]})
        assert answer.status_code == 200


def test_signed_out(app_env):
    client, _, _ = app_env
    me = client.get("/api/me").json()
    assert me["user"] is None and me["signin"]["email"] is True
    assert client.get("/api/chats").status_code == 401


def test_progress_recovers_events_without_restarting_a_run(app_env):
    from pantree_insights import web

    client, codes, _ = app_env
    sign_in(client, codes)
    chat = client.post("/api/chats", json={}).json()
    run_id = client.post(f"/api/chats/{chat['id']}/ask", json={
        "question": "Revenue by category", "use_defaults": True,
    }).json()["run_id"]
    path = f"/api/runs/{run_id}/progress"
    response = client.get(path, params={"since": 1})
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    assert response.json()["events"]
    assert web.runner.runs[run_id].segment_done.wait(15)
    pending = client.get(path).json()["events"][-1]
    assert pending["kind"] == "confirm"
    events = run_to_end(client, run_id)
    assert events[0]["step"] == "prepare" and events[-1]["status"] == "done"
    assert client.get(path, params={"since": len(events) - 1}).json() == {"events": events[-1:], "active": False}
    web.runner.runs.pop(run_id)
    assert client.get(path, params={"since": len(events) - 1}).json() == {"events": events[-1:], "active": False}
    sign_in(client, codes, "eve@example.com")
    assert client.get(path).status_code == 404


def test_email_sign_in_rules(app_env):
    client, codes, _ = app_env
    assert client.post("/auth/email/request", json={"email": "not-an-email"}).status_code == 400
    assert client.post("/auth/email/request", json={"email": "ana@example.com"}).status_code == 200
    assert client.post("/auth/email/request", json={"email": "ana@example.com"}).status_code == 429   # 60s limit
    assert client.post("/auth/email/verify", json={"email": "ana@example.com", "code": "000000"}).status_code == 400
    assert client.post("/auth/email/verify", json={"email": "ana@example.com",
                                                   "code": codes["ana@example.com"]}).status_code == 200
    assert client.get("/api/me").json()["user"]["email"] == "ana@example.com"


def test_writes_must_be_json(app_env):
    client, codes, _ = app_env
    sign_in(client, codes)
    assert client.post("/api/projects", data={"name": "x"}).status_code == 415


def test_ask_in_project_streams_and_remembers(app_env):
    client, codes, _ = app_env
    sign_in(client, codes)
    project = client.post("/api/projects", json={"name": "Festive", "instructions": "Focus on Diwali"}).json()
    chat = client.post("/api/chats", json={"project_id": project["id"]}).json()
    run_id = client.post(f"/api/chats/{chat['id']}/ask", json={"question": "Revenue by category?"}).json()["run_id"]
    events = run_to_end(client, run_id)
    final = events[-1]
    assert final["type"] == "final" and final["status"] == "done"
    assert any(e.get("step") == "execute" for e in events) and final["trust"] == "verified"
    detail = client.get(f"/api/chats/{chat['id']}").json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant", "user", "assistant"]
    assert detail["title"] == "Revenue by category?"
    assert detail["messages"][-1]["run"]["events"]                       # saved for replay in the panel
    memory = client.get("/api/memory").json()
    assert memory["insights"][0]["scope"] == "project" and memory["insights"][0]["project_name"] == "Festive"
    # second question in the project: the interpreter sees project instructions, insight and chat history
    run_id = client.post(f"/api/chats/{chat['id']}/ask", json={"question": "And by month?"}).json()["run_id"]
    run_to_end(client, run_id)
    prompt = [u for a, u in PROMPTS if a == "interpreter"][-1]
    assert "Focus on Diwali" in prompt
    assert "Earlier insights in this project" in prompt
    assert "User: Revenue by category?" in prompt


def test_clarify_then_answer(app_env):
    client, codes, _ = app_env
    sign_in(client, codes)
    chat = client.post("/api/chats", json={}).json()
    question = {"question": "clarify me: how are sales?"}
    run_id = client.post(f"/api/chats/{chat['id']}/ask", json=question).json()["run_id"]
    events = run_to_end(client, run_id)
    assert events[-1]["type"] == "clarify"
    assert client.post(f"/api/runs/{run_id}/answer", json={
        "message": "Orders", "plan_version": events[-1]["plan_version"]}).status_code == 200
    rest = run_to_end(client, run_id, since=len(events))
    assert rest[0]["type"] == "answered" and rest[-1]["status"] == "done"


def test_daily_limit_and_privacy(app_env):
    client, codes, _ = app_env
    sign_in(client, codes)
    chat = client.post("/api/chats", json={}).json()
    for _ in range(3):
        run_to_end(client, client.post(f"/api/chats/{chat['id']}/ask", json={"question": "Revenue?"}).json()["run_id"])
    assert client.post(f"/api/chats/{chat['id']}/ask", json={"question": "Revenue?"}).status_code == 429
    client.post("/auth/logout", json={})
    sign_in(client, codes, "eve@example.com")
    assert client.get(f"/api/chats/{chat['id']}").status_code == 404     # another user's chat
    assert client.get("/api/memory").json()["insights"] == []


def test_admin_shares_business_memory(app_env):
    client, codes, _ = app_env
    sign_in(client, codes, "boss@example.com")
    assert client.get("/api/me").json()["limit"]["unlimited"] is True
    chat = client.post("/api/chats", json={}).json()
    run_to_end(client, client.post(f"/api/chats/{chat['id']}/ask", json={"question": "Revenue?"}).json()["run_id"])
    mem = client.get("/api/memory").json()
    assert client.post(f"/api/memory/{mem['insights'][0]['id']}/promote", json={}).status_code == 200
    client.post("/auth/logout", json={})
    sign_in(client, codes, "eve@example.com")
    assert len(client.get("/api/memory").json()["business"]) == 1     # shared with everyone
    assert client.post(f"/api/memory/{mem['insights'][0]['id']}/promote", json={}).status_code == 403


def test_scoped_feedback_and_admin_observability(app_env):
    client, codes, _ = app_env
    sign_in(client, codes)
    chat = client.post("/api/chats", json={}).json()
    events = run_to_end(
        client, client.post(f"/api/chats/{chat['id']}/ask",
                            json={"question": "Revenue by category?"}).json()["run_id"]
    )
    final = events[-1]
    assert final["status"] == "done"
    payload = {"chat_id": chat["id"], "run_id": final["run_id"], "rating": "negative",
               "category": "wrong_metric", "comment": "Please review the metric"}
    assert client.post("/api/feedback", json=payload).status_code == 200
    assert client.post("/api/feedback", json={**payload, "run_id": "missing"}).status_code == 404
    assert client.get("/api/admin/feedback").status_code == 403
    assert client.get("/api/admin/metrics").status_code == 403
    client.post("/auth/logout", json={})
    sign_in(client, codes, "boss@example.com")
    rows = client.get("/api/admin/feedback").json()["feedback"]
    assert len(rows) == 1 and rows[0]["category"] == "wrong_metric"
    assert "user_id" not in rows[0] and "question" not in rows[0]
    metrics = client.get("/api/admin/metrics").json()
    assert metrics["completed_runs"] >= 1
    assert metrics["status_counts"]["done"] >= 1



def test_demo_replay(app_env):
    client, _, tmp_path = app_env
    demo = {"question": "Demo Q", "data_size": "small", "data_as_of": "2026-11-14", "seconds": 1, "events": [
        {"type": "step", "step": "plan", "status": "started", "t": 0.0},
        {"type": "step", "step": "plan", "status": "done", "t": 0.1, "detail": {}},
        {"type": "final", "status": "done", "t": 0.2}]}
    (tmp_path / "demos").mkdir()
    (tmp_path / "demos" / "demo-q.json").write_text(json.dumps(demo))
    assert client.get("/api/demos").json()[0]["slug"] == "demo-q"
    with client.stream("GET", "/api/demos/demo-q/stream") as r:
        lines = [json.loads(x[6:]) for x in r.iter_lines() if x.startswith("data: ")]
    assert [e["type"] for e in lines] == ["step", "step", "final"] and all(e["replay"] for e in lines)


def test_readback_is_sent_to_the_browser_and_confirming_finishes(app_env):

    client, codes, _ = app_env
    sign_in(client, codes)
    chat = client.post("/api/chats", json={}).json()
    run_id = client.post(f"/api/chats/{chat['id']}/ask",
                         json={"question": "Break down revenue by category"}).json()["run_id"]
    events = run_to_end(client, run_id, confirm=False)
    assert events[-1]["kind"] == "confirm" and "**Measure:**" in events[-1]["readback"]
    client.post(f"/api/runs/{run_id}/answer", json={"message": "Go ahead", "plan_version": events[-1]["plan_version"]})
    assert run_to_end(client, run_id, since=len(events))[-1]["status"] == "done"


def test_a_slow_cold_start_never_blocks_the_ask_request(app_env, con):
    """The first question after a restart builds the agent (data profile, catalog): seconds to minutes on a cold
    instance. The request must return at once and the build shows as a step, not as a browser timeout."""
    import time

    from pantree_insights import web
    from pantree_insights.graph import Agent
    from pantree_insights.llm import FakeLLM

    def slow_agent():
        time.sleep(3)
        return Agent(llm_factory=lambda: FakeLLM(responder), con=con)

    web.runner = web.Runner(slow_agent)
    client, codes, _ = app_env
    sign_in(client, codes)
    chat = client.post("/api/chats", json={}).json()
    started = time.time()
    response = client.post(f"/api/chats/{chat['id']}/ask", json={"question": "Revenue by category?"})
    assert response.status_code == 200 and time.time() - started < 1.5
    events = run_to_end(client, response.json()["run_id"])
    steps = [e.get("step") for e in events if e.get("type") == "step"]
    assert steps[0] == "prepare" and events[-1]["status"] == "done"


def test_a_failure_while_preparing_still_answers_the_question(app_env):
    from pantree_insights import web

    def broken():
        raise RuntimeError("data mount unavailable")

    web.runner = web.Runner(broken)
    client, codes, _ = app_env
    sign_in(client, codes)
    chat = client.post("/api/chats", json={}).json()
    run_id = client.post(f"/api/chats/{chat['id']}/ask", json={"question": "Revenue?"}).json()["run_id"]
    events = run_to_end(client, run_id)
    assert events[-1]["type"] == "final" and events[-1]["status"] == "failed"
    detail = client.get(f"/api/chats/{chat['id']}").json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]          # never left unanswered


@pytest.mark.parametrize("secret", ["", "short", " " * 40, "dev-only" + "x" * 40])
def test_production_rejects_missing_or_weak_session_secrets(monkeypatch, secret):
    from pantree_insights import web
    from pantree_insights.config import Settings
    monkeypatch.setattr(web, "get_settings", lambda: Settings(app_env="production", session_secret=secret))
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        web.create_app()


def test_terminal_event_waits_for_durable_result(monkeypatch):
    import threading

    from pantree_insights.web import Run, Runner
    runner = Runner(lambda: object())
    run = Run("persist", {}, {}, "question")
    saving, release = threading.Event(), threading.Event()
    def delayed_save(*args):
        saving.set()
        assert release.wait(3)
    monkeypatch.setattr(runner, "_save", delayed_save)
    runner._consume(None, run, iter([{"type": "final", "status": "done"}]))
    try:
        assert saving.wait(3)
        assert not run.events and not run.finished
    finally:
        release.set()
    assert run.segment_done.wait(3)
    assert run.finished and run.events[-1]["type"] == "final"


def test_ask_retries_are_idempotent_and_payload_conflicts_are_rejected(app_env):
    from pantree_insights.store import get_store
    client, codes, _ = app_env
    sign_in(client, codes)
    chat = client.post('/api/chats', json={}).json()
    body = {'question': 'Show orders', 'idempotency_key': 'client-retry-123', 'use_defaults': True}
    url = f"/api/chats/{chat['id']}/ask"
    first = client.post(url, json=body).json()
    assert client.post(url, json=body).json() == first
    assert run_to_end(client, first['run_id'])[-1]['status'] == 'done'
    assert client.post(url, json=body).json() == first
    assert client.post(url, json={**body, 'question': 'Show revenue'}).status_code == 409
    assert client.post(url, json={**body, 'question': 'Show revenue'}).status_code == 409
    assert len(get_store().messages(chat['id'])) == 4


def test_normal_composer_revisions_retries_and_stale_confirmation(app_env):
    from pantree_insights import web
    from pantree_insights.store import get_store
    client, codes, _ = app_env
    sign_in(client, codes)
    chat = client.post('/api/chats', json={}).json()
    url = f"/api/chats/{chat['id']}/ask"
    run_id = client.post(url, json={'question': 'Show revenue by category'}).json()['run_id']
    first = run_to_end(client, run_id, confirm=False)
    reply = {'question': 'No, by brand instead', 'pending_run_id': run_id,
             'plan_version': first[-1]['plan_version'], 'idempotency_key': 'revision-once'}
    response = client.post(url, json=reply)
    assert response.status_code == 200
    assert client.post(url, json=reply).json() == response.json()
    second = run_to_end(client, run_id, since=response.json()['since'], confirm=False)
    assert second[-1]['spec']['dimensions'] == ['brand']
    assert not any(e['type'] == 'result' for e in web.runner.runs[run_id].events)
    stale = {**reply, 'question': 'Yes', 'idempotency_key': 'stale-confirm'}
    assert client.post(url, json=stale).status_code == 409
    approve = {**reply, 'question': 'Go ahead', 'plan_version': second[-1]['plan_version'],
               'idempotency_key': 'confirm-once'}
    approved = client.post(url, json=approve).json()
    final = run_to_end(client, run_id, since=approved['since'])[-1]
    assert final['status'] == 'done' and final['spec'] == second[-1]['spec']
    assert client.post(url, json=approve).json() == approved
    web.runner.runs.pop(run_id)  # a retry after process state was discarded still cannot execute again
    assert client.post(url, json=approve).json() == approved
    messages = get_store().messages(chat['id'])
    assert [m['content'] for m in messages if m['role'] == 'user'] == [
        'Show revenue by category', 'No, by brand instead', 'Go ahead']
