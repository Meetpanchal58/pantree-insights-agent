"""AI-account and dataset endpoint authorization, secret redaction and model pinning."""

from cryptography.fernet import Fernet
from pydantic import SecretStr
from test_web import app_env as app_env
from test_web import run_to_end, sign_in

from pantree_insights import providers
from pantree_insights.config import get_settings
from pantree_insights.store import get_store


def setup_provider(monkeypatch):
    monkeypatch.setattr(get_settings(), "credential_encryption_key", SecretStr(Fernet.generate_key().decode()))
    monkeypatch.setenv("OPENAI_API_KEY", "shared-test-key")
    monkeypatch.setattr(providers, "discover", lambda *a, **kw: [{"id": "model-one"}, {"id": "model-two"}])


def test_dataset_is_authorized_free_and_preserves_pending_plan(app_env):
    client, codes, _ = app_env
    assert client.get("/api/dataset").status_code == 401
    sign_in(client, codes)
    chat = client.post("/api/chats", json={}).json()
    run_id = client.post(f"/api/chats/{chat['id']}/ask", json={"question": "Show orders"}).json()["run_id"]
    plan = run_to_end(client, run_id, confirm=False)[-1]
    before = client.get("/api/me").json()
    metadata = client.get("/api/dataset").json()
    table = client.get("/api/dataset/sales", params={"version": metadata["version"]})
    assert table.status_code == 200 and len(table.json()["rows"]) == 30
    after = client.get("/api/me").json()
    assert before["limit"] == after["limit"] and before["ai"]["usage"] == after["ai"]["usage"]
    from pantree_insights import web

    assert web.runner.runs[run_id].paused and web.runner.runs[run_id].events[-1]["plan_version"] == plan["plan_version"]
    assert client.get("/api/dataset/customer_pii").status_code == 404


def test_ordinary_users_switch_models_without_approval_and_pending_run_stays_pinned(app_env, monkeypatch):
    client, codes, _ = app_env
    sign_in(client, codes)
    setup_provider(monkeypatch)
    for model in ("model-one", "model-two"):
        assert (
            client.put("/api/ai/settings", json={"mode": "shared", "provider": "openai", "model": model}).status_code
            == 200
        )
    chat = client.post("/api/chats", json={}).json()
    run_id = client.post(f"/api/chats/{chat['id']}/ask", json={"question": "Show orders"}).json()["run_id"]
    run_to_end(client, run_id, confirm=False)
    client.put("/api/ai/settings", json={"mode": "shared", "provider": "openai", "model": "model-one"})
    user_id = client.get("/api/me").json()["user"]["id"]
    assert get_store().run_record(user_id, run_id)["summary"]["ai"]["model"] == "model-two"
    assert client.get("/api/ai/settings").json()["model"] == "model-one"
    assert (
        client.put(
            "/api/ai/settings", json={"mode": "shared", "provider": "openai", "model": "not-available"}
        ).status_code
        == 400
    )
    assert client.get("/api/admin/usage").status_code == 403


def test_credential_responses_never_echo_keys_including_validation_errors(app_env, monkeypatch):
    client, codes, _ = app_env
    sign_in(client, codes)
    setup_provider(monkeypatch)
    key = "sensitive-api-key-for-test"
    body = {"mode": "personal", "provider": "groq", "model": "model-one", "api_key": key}
    assert client.post("/api/ai/test", json={"provider": "groq", "api_key": key}).status_code == 200
    response = client.put("/api/ai/settings", json=body)
    assert response.status_code == 200 and key not in response.text
    assert key not in client.get("/api/ai/settings").text and key not in client.get("/api/me").text
    assert client.get("/api/ai/settings").json()["exempt"]
    bad = client.put("/api/ai/settings", json={**body, "api_key": key * 100})
    assert bad.status_code == 422 and key not in bad.text
    assert client.request("DELETE", "/api/ai/credentials", json={}).status_code == 200
    assert client.get("/api/ai/settings").json()["personal"] is None


def test_admin_manages_allowances_but_other_users_cannot(app_env, monkeypatch):
    client, codes, _ = app_env
    sign_in(client, codes)
    ordinary = client.get("/api/me").json()["user"]["id"]
    path = f"/api/admin/usage/{ordinary}"
    assert client.put(path, json={"daily_limit": 2.5}).status_code == 403
    sign_in(client, codes, "boss@example.com")
    setup_provider(monkeypatch)
    assert client.put(path, json={"daily_limit": 2.5}).status_code == 200
    assert get_store().ai_profile(ordinary)["daily_limit"] == 2.5
    assert client.get("/api/ai/settings").json()["exempt"]
    assert client.get("/api/admin/usage").status_code == 200


def test_readiness_is_authenticated_reuses_preparation_and_spends_no_allowance(app_env):
    client, codes, _ = app_env
    assert client.get("/api/ready").status_code == 401
    sign_in(client, codes)
    before = client.get("/api/me").json()
    assert client.get("/api/ready").json()["ready"]
    from pantree_insights import web

    agent = web.runner.agent
    assert client.get("/api/ready").json()["ready"] and web.runner.agent is agent
    after = client.get("/api/me").json()
    assert before["limit"] == after["limit"] and before["ai"]["usage"] == after["ai"]["usage"]


def test_readiness_failure_is_visible_without_internal_details(app_env, monkeypatch):
    client, codes, _ = app_env
    sign_in(client, codes)
    from pantree_insights import web

    def fail():
        raise RuntimeError("private infrastructure details")

    monkeypatch.setattr(web.runner, "_agent_factory", fail)
    response = client.get("/api/ready")
    assert response.status_code == 503 and "private infrastructure" not in response.text
