"""The web app end to end (sign-in, chats, projects, live runs, memory, demos) with a fake LLM."""
import json
import shutil

import pytest

from pantree_insights.config import ROOT

pytestmark = pytest.mark.data

GOOD_SQL = ("SELECT p.category, SUM(CASE WHEN s.delivered_at IS NOT NULL THEN s.net_amount - "
            "COALESCE(s.refund_amount, 0) ELSE 0 END) AS realized_revenue, COUNT(DISTINCT s.order_id) AS orders "
            "FROM sales s JOIN product_master p ON p.product_id = s.product_id WHERE s.line_type = 'Sale' "
            "GROUP BY 1 ORDER BY realized_revenue DESC")
PLAN = {"decision": "proceed", "restated_question": "Realized revenue by category", "metrics": ["realized_revenue"],
        "breakdowns": ["category"], "time_window": "all data", "output_shape": "one row per category"}
CLARIFY = {"decision": "clarify", "restated_question": "Sales", "clarifying_questions": [
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


FRAME = {"restated_question": "Realized revenue by category", "subjects": ["orders"],
         "measures": ["realized_revenue"], "breakdowns": ["category"]}


def responder(agent, schema, system, user):
    PROMPTS.append((agent, user))
    if agent == "understander":                  # the understanding step asks; the planner no longer does
        if "clarify me" in user and "answers so far" not in user:
            return {**FRAME, "open_points": CLARIFY["clarifying_questions"]}
        return FRAME
    if agent == "planner":
        return PLAN
    return {"sql_writer": {"sql": GOOD_SQL, "explanation": "x"}, "validator": {"ok": True}}.get(agent) or analysis(user)


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
    from pantree_insights.retrieval import Retriever
    retriever = Retriever(con)
    web.runner = web.Runner(lambda: Agent(llm_factory=lambda: FakeLLM(responder), retriever=retriever, con=con))
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
    from pantree_insights.graph import CONFIRM_QUESTION, CONFIRM_YES

    events = []
    while True:
        with client.stream("GET", f"/api/runs/{run_id}/stream?since={since + len(events)}") as r:
            for line in r.iter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
        last = events[-1] if events else {}
        if not (confirm and last.get("type") == "clarify" and last.get("kind") == "confirm"):
            return events
        answer = client.post(f"/api/runs/{run_id}/answer", json={"answers": {CONFIRM_QUESTION: CONFIRM_YES}})
        assert answer.status_code == 200


def test_signed_out(app_env):
    client, _, _ = app_env
    me = client.get("/api/me").json()
    assert me["user"] is None and me["signin"]["email"] is True
    assert client.get("/api/chats").status_code == 401


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
    assert any(e.get("step") == "write_sql" for e in events)
    detail = client.get(f"/api/chats/{chat['id']}").json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert detail["title"] == "Revenue by category?"
    assert detail["messages"][1]["run"]["events"]                       # saved for replay in the panel
    memory = client.get("/api/memory").json()
    assert memory["insights"][0]["scope"] == "project" and memory["insights"][0]["project_name"] == "Festive"
    # second question in the project: planner sees project instructions, insight and chat history
    run_id = client.post(f"/api/chats/{chat['id']}/ask", json={"question": "And by month?"}).json()["run_id"]
    run_to_end(client, run_id)
    planner_prompt = [u for a, u in PROMPTS if a == "planner"][-1]
    assert "Focus on Diwali" in planner_prompt
    assert "Earlier insights in this project" in planner_prompt
    assert "User: Revenue by category?" in planner_prompt


def test_clarify_then_answer(app_env):
    client, codes, _ = app_env
    sign_in(client, codes)
    chat = client.post("/api/chats", json={}).json()
    question = {"question": "clarify me: how are sales?"}
    run_id = client.post(f"/api/chats/{chat['id']}/ask", json=question).json()["run_id"]
    events = run_to_end(client, run_id)
    assert events[-1]["type"] == "clarify"
    assert client.post(f"/api/runs/{run_id}/answer", json={"answers": {"Which measure?": "Orders"}}).status_code == 200
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
    from pantree_insights.graph import CONFIRM_QUESTION, CONFIRM_YES

    client, codes, _ = app_env
    sign_in(client, codes)
    chat = client.post("/api/chats", json={}).json()
    run_id = client.post(f"/api/chats/{chat['id']}/ask", json={"question": "Revenue by category?"}).json()["run_id"]
    events = run_to_end(client, run_id, confirm=False)
    assert events[-1]["kind"] == "confirm" and "**Measure:**" in events[-1]["readback"]
    client.post(f"/api/runs/{run_id}/answer", json={"answers": {CONFIRM_QUESTION: CONFIRM_YES}})
    assert run_to_end(client, run_id, since=len(events))[-1]["status"] == "done"
