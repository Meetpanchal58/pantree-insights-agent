"""Sent-message edits preserve audit history and never reuse approval for a changed calculation."""

from test_web import app_env as app_env
from test_web import run_to_end, sign_in

from pantree_insights.editing import reconstruction
from pantree_insights.store import Store


def request(client, chat, question="Revenue?"):
    response = client.post(f"/api/chats/{chat}/ask", json={"question": question})
    assert response.status_code == 200, response.text
    return response.json()


def edit(client, chat, mid, content, original, key="editing-key-001", **extra):
    return client.post(
        f"/api/chats/{chat}/messages/{mid}/edit",
        json={"content": content, "expected_content": original, "idempotency_key": key, **extra},
    )


def test_typo_then_edited_go_ahead_runs_once_without_new_question(app_env):
    client, codes, _ = app_env
    sign_in(client, codes)
    chat = client.post("/api/chats", json={}).json()["id"]
    first = request(client, chat)
    plan = run_to_end(client, first["run_id"], confirm=False)[-1]
    typo = client.post(
        f"/api/chats/{chat}/ask",
        json={
            "question": "g",
            "pending_run_id": first["run_id"],
            "plan_version": plan["plan_version"],
            "idempotency_key": "typo-message-001",
        },
    ).json()
    again = run_to_end(client, first["run_id"], since=typo["since"], confirm=False)[-1]
    assert again["kind"] == "confirm" and "looks incomplete" in again["message"]
    before = client.get("/api/me").json()["limit"]["used"]
    body = {"pending_run_id": first["run_id"], "plan_version": again["plan_version"]}
    edited = edit(client, chat, typo["message_id"], "go ahead", "g", **body)
    assert edited.status_code == 200, edited.text
    final = run_to_end(client, first["run_id"], since=edited.json()["since"])[-1]
    assert final["status"] == "done"
    assert edit(client, chat, typo["message_id"], "go ahead", "g", **body).json() == edited.json()
    messages = client.get(f"/api/chats/{chat}").json()["messages"]
    revised = next(m for m in messages if m["id"] == typo["message_id"])
    assert revised["content"] == "go ahead" and revised["run"]["edits"][0]["content"] == "g"
    assert client.get("/api/me").json()["limit"]["used"] == before


def test_completed_question_edit_forks_before_downstream_answers_and_is_idempotent(app_env):
    client, codes, _ = app_env
    sign_in(client, codes)
    chat = client.post("/api/chats", json={}).json()["id"]
    first = request(client, chat)
    run_to_end(client, first["run_id"])
    second = request(client, chat, "Orders?")
    run_to_end(client, second["run_id"])
    original = client.get(f"/api/chats/{chat}").json()["messages"]
    edited = edit(client, chat, first["message_id"], "Revenue by brand", "Revenue?")
    assert edited.status_code == 200, edited.text
    branch = edited.json()["chat_id"]
    assert branch != chat and edited.json()["mode"] == "fork"
    proposal = run_to_end(client, edited.json()["run_id"], confirm=False)[-1]
    assert proposal["kind"] == "confirm" and proposal["spec"]["dimensions"] == ["brand"]
    assert client.get(f"/api/chats/{chat}").json()["messages"] == original
    branch_messages = client.get(f"/api/chats/{branch}").json()["messages"]
    assert branch_messages[0]["content"] == "Revenue by brand"
    assert not any(m.get("run", {}).get("status") == "done" for m in branch_messages if m.get("run"))
    used = client.get("/api/me").json()["limit"]["used"]
    assert edit(client, chat, first["message_id"], "Revenue by brand", "Revenue?").json() == edited.json()
    assert client.get("/api/me").json()["limit"]["used"] == used
    assert edit(client, chat, first["message_id"], "Orders by brand", "Revenue?").status_code == 409


def test_edits_reject_foreign_assistant_and_stale_messages(app_env):
    client, codes, _ = app_env
    sign_in(client, codes)
    chat = client.post("/api/chats", json={}).json()["id"]
    first = request(client, chat)
    run_to_end(client, first["run_id"])
    assistant = next(m for m in client.get(f"/api/chats/{chat}").json()["messages"] if m["role"] == "assistant")
    assert edit(client, chat, assistant["id"], "changed", assistant["content"]).status_code == 404
    assert edit(client, chat, first["message_id"], "Orders", "wrong original").status_code == 409
    assert edit(client, chat, first["message_id"], "   ", "Revenue?").status_code == 422
    client.post("/auth/logout", json={})
    sign_in(client, codes, "another@example.test")
    assert edit(client, chat, first["message_id"], "Orders", "Revenue?").status_code == 404


def test_clarification_reconstruction_retains_metric_answer_but_not_approval():
    prefix = [
        {"role": "user", "content": "Top brands"},
        {
            "role": "assistant",
            "run": {"type": "clarify", "questions": [{"question": "Rank by what?", "options": ["Revenue", "Orders"]}]},
        },
        {"role": "user", "content": "1"},
        {"role": "assistant", "run": {"type": "clarify", "kind": "confirm", "questions": []}},
    ]
    seed = reconstruction(prefix, "go ahead")
    assert seed == {"question": "Top brands", "answers": {"Rank by what?": "Revenue"}}
    assert reconstruction(prefix[:2], "Orders")["answers"] == {"Rank by what?": "Orders"}


def test_fork_keeps_only_prior_verified_context_and_archived_runs(tmp_path):
    from test_adaptive_investigations import save

    store = Store(f"sqlite:///{tmp_path}/edits.db")
    user = store.upsert_user("editor@example.test")
    chat = store.create_chat(user["id"])
    try:
        save(store, user, chat, "abcdef123456", "Earlier footwear refund variance")
        target = store.add_message(chat["id"], "user", "Revenue?")
        save(store, user, chat, "abcdef123457", "Later unrelated sales analysis")
        branch = store.fork_message(user["id"], chat["id"], target, "Orders?", "Revenue?", "fork-key-001")
        recalled = store.recall(user["id"], branch["chat_id"], "footwear refund", "snapshot-a")
        assert recalled["retrieved_evidence"] and recalled["historical_populations"][0]["complete"]
        assert not store.recall(user["id"], branch["chat_id"], "unrelated sales", "snapshot-a")["retrieved_evidence"]
        assert store.investigation(user["id"], branch["chat_id"])["populations"][0]["values"] == ["ADIDAS"]
        assert len(store.messages(chat["id"])) == 3
    finally:
        store.engine.dispose()


def test_editing_completed_clarification_rebuilds_original_request(app_env):
    client, codes, _ = app_env
    sign_in(client, codes)
    chat = client.post("/api/chats", json={}).json()["id"]
    first = request(client, chat, "clarify me: how are sales?")
    clarify = run_to_end(client, first["run_id"], confirm=False)[-1]
    answer = client.post(
        f"/api/chats/{chat}/ask",
        json={"question": "Orders", "pending_run_id": first["run_id"], "plan_version": clarify["plan_version"]},
    ).json()
    run_to_end(client, first["run_id"], since=answer["since"])
    edited = edit(client, chat, answer["message_id"], "Realized revenue", "Orders")
    assert edited.status_code == 200, edited.text
    proposal = run_to_end(client, edited.json()["run_id"], confirm=False)[-1]
    assert proposal["kind"] == "confirm"
    assert proposal["spec"]["measures"][0]["metric"] == "realized_revenue"
    assert not any(
        e.get("type") == "result"
        for e in client.get(f"/api/chats/{edited.json()['chat_id']}")
        .json()["messages"][-1]
        .get("run", {})
        .get("events", [])
    )


def test_unknown_revision_can_recover_unchanged_plan_without_executing(con, tmp_path, monkeypatch):
    from test_agent import BY_CATEGORY, Script, make_agent

    agent, _ = make_agent(con, tmp_path, Script(interpreter=[BY_CATEGORY, {"question": "What change do you want?"}]))
    queries = []
    execute = agent.query
    monkeypatch.setattr(agent, "query", lambda sql: queries.append(sql) or execute(sql))
    try:
        rid, stream = agent.start("Revenue split?")
        first = list(stream)[-1]
        unknown = list(agent.resume(rid, "change it", first["plan_version"]))[-1]
        assert unknown["kind"] == "clarify" and not queries
        recovered = list(agent.resume(rid, "go ahead", unknown["plan_version"]))[-1]
        assert recovered["kind"] == "confirm" and "unchanged plan" in recovered["message"] and not queries
        assert recovered["plan_version"] > first["plan_version"]
        final = list(agent.resume(rid, "go ahead", recovered["plan_version"]))[-1]
        assert final["status"] == "done" and len(queries) == 1
    finally:
        agent.close()


def test_go_ahead_does_not_answer_a_genuine_unresolved_question(con, tmp_path):
    from test_agent import Script, make_agent
    from test_web import CLARIFY

    agent, _ = make_agent(con, tmp_path, Script(interpreter=[CLARIFY]))
    try:
        rid, stream = agent.start("What should we investigate?")
        first = list(stream)[-1]
        still_open = list(agent.resume(rid, "go ahead", first["plan_version"]))[-1]
        assert still_open["kind"] == "clarify" and "Please answer the open question" in still_open["message"]
        assert not still_open["plan"]
    finally:
        agent.close()


def test_superseded_clarification_is_audit_only(tmp_path):
    store = Store(f"sqlite:///{tmp_path}/audit.db")
    user = store.upsert_user("audit@example.test")
    chat = store.create_chat(user["id"])
    try:
        store.add_message(chat["id"], "user", "Revenue?")
        store.add_message(chat["id"], "assistant", "Obsolete clarification",
                          {"type": "clarify", "superseded_by_edit": "edited"})
        assert "Obsolete" not in store.conversation(chat["id"])[0]
        assert reconstruction(store.messages(chat["id"]), "Orders?") == {"question": "Orders?", "answers": {}}
    finally:
        store.engine.dispose()


def test_executed_approval_edit_requires_new_plan_confirmation(app_env):
    client, codes, _ = app_env
    sign_in(client, codes)
    chat = client.post("/api/chats", json={}).json()["id"]
    first = request(client, chat)
    run_to_end(client, first["run_id"])
    original = client.get(f"/api/chats/{chat}").json()["messages"]
    approval = next(m for m in original if m["role"] == "user" and m["content"] == "Go ahead")
    edited = edit(client, chat, approval["id"], "go ahead", "Go ahead")
    assert edited.status_code == 200, edited.text
    assert edited.json()["mode"] == "fork"
    plan = run_to_end(client, edited.json()["run_id"], confirm=False)[-1]
    assert plan["kind"] == "confirm", plan
    assert client.get(f"/api/chats/{chat}").json()["messages"] == original
