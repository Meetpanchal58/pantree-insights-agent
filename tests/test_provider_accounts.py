"""Account isolation, pinned selection and atomic cost reservations; provider contracts use local responses."""

from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import BaseModel, SecretStr

from pantree_insights import providers
from pantree_insights.config import get_settings
from pantree_insights.llm import BUDGET_KEY, BudgetExceeded, ChatLLM
from pantree_insights.store import Store


@pytest.fixture
def account(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "credential_encryption_key", SecretStr(Fernet.generate_key().decode()))
    monkeypatch.setattr(settings, "daily_budget_usd", 1.0)
    monkeypatch.setattr(settings, "daily_user_budget_usd", 1.0)
    monkeypatch.setenv("OPENAI_API_KEY", "shared-test-key-not-real")
    store = Store(f"sqlite:///{tmp_path}/accounts.db")
    user = store.upsert_user("first@example.test")
    yield store, user
    store.engine.dispose()


def test_encrypted_keys_never_cross_users_and_replacement_revokes_pending_run(account):
    store, user = account
    other = store.upsert_user("other@example.test")
    key = "personal-test-secret-key"
    cid = store.add_credential(user["id"], "groq", key, "llama-3.3-70b-versatile")
    pin = store.pin_ai(user)
    encrypted = store.credential(user["id"], cid)
    assert key not in encrypted["ciphertext"] and store.credential(other["id"], cid) is None
    assert store.ai_key(pin) == key
    with pytest.raises(providers.ProviderError):
        providers.decrypt_key(other["id"], encrypted)
    store.add_credential(user["id"], "groq", "replacement-test-secret", pin["model"])
    with pytest.raises(providers.ProviderError, match="revoked"):
        store.ai_key(pin)
    new = store.pin_ai(user)
    store.remove_credentials(user["id"])
    with pytest.raises(providers.ProviderError, match="revoked"):
        store.ai_key(new)
    assert store.ai_profile(user["id"])["mode"] == "shared"


def test_model_switch_preserves_pinned_configuration(account):
    store, user = account
    pin = store.pin_ai(user)
    store.set_ai_profile(user["id"], model="gpt-6.1-sol")
    assert pin["model"] != store.pin_ai(user)["model"]
    assert store.ai_key(pin) == "shared-test-key-not-real"


def test_shared_key_rotation_cannot_change_an_existing_run(account, monkeypatch):
    store, user = account
    pin = store.pin_ai(user)
    monkeypatch.setenv("OPENAI_API_KEY", "rotated-shared-key")
    with pytest.raises(providers.ProviderError, match="changed"):
        store.ai_key(pin)


def test_concurrent_reservations_enforce_combined_user_and_global_budget(account):
    store, user = account
    pin = store.pin_ai(user)

    def reserve(i):
        try:
            return store.reserve_api_call(
                {**pin, "model": "expensive" if i % 2 else "cheap"}, f"r{i}", "interpreter", 0.2
            )
        except BudgetExceeded:
            return None

    with ThreadPoolExecutor(max_workers=12) as pool:
        accepted = [cid for cid in pool.map(reserve, range(30)) if cid]
    assert len(accepted) == 5
    assert store.usd_today(user["id"]) == pytest.approx(1)
    assert store.usd_today(BUDGET_KEY) == pytest.approx(1)
    # Reconciliation refunds unused reservation exactly once, even under retries.
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: store.settle_api_call(accepted[0], 0.05, {"input": 5, "output": 2}), range(16)))
    assert store.usd_today(user["id"]) == pytest.approx(0.85)
    assert store.usd_today(BUDGET_KEY) == pytest.approx(0.85)
    store.settle_api_call(accepted[1], None, {}, "uncertain")
    assert store.usd_today(user["id"]) == pytest.approx(0.85)


def test_global_budget_rollback_does_not_charge_a_user(account):
    store, user = account
    store.add_usd_today(BUDGET_KEY, 1)
    with pytest.raises(BudgetExceeded):
        store.reserve_api_call(store.pin_ai(user), "r", "analyst", 0.1)
    assert store.usd_today(user["id"]) == 0
    assert store.api_usage(user["id"])["groups"] == []


def test_admin_and_personal_calls_are_tracked_without_ordinary_quotas(account, monkeypatch):
    store, user = account
    monkeypatch.setattr(get_settings(), "daily_budget_usd", 0)
    store.set_ai_profile(user["id"], daily_limit=0)
    for billing in ["admin", "personal"]:
        pin = {**store.pin_ai(user), "billing": billing}
        cid = store.reserve_api_call(pin, billing, "analyst", 50)
        store.settle_api_call(cid, 20, {"input": 100, "cached": 40, "output": 20})
    assert store.usd_today(user["id"]) == store.usd_today(BUDGET_KEY) == 0
    groups = store.api_usage(user["id"])["groups"]
    assert {g["billing"] for g in groups} == {"admin", "personal"}
    assert sum(g["usd"] for g in groups) == 40


class Answer(BaseModel):
    answer: str


@pytest.mark.parametrize(
    "provider,model",
    [
        ("openai", "gpt-4.1"),
        ("groq", "llama-3.3-70b-versatile"),
        ("anthropic", "claude-sonnet-4-20250514"),
        ("gemini", "gemini-2.5-flash"),
    ],
)
def test_provider_wire_contract_and_usage(provider, model, monkeypatch):
    captured = []

    def request(method, url, **kwargs):
        captured.append((method, url, kwargs))
        if provider == "openai":
            result = {
                "output": [{"type": "function_call", "name": "respond", "arguments": '{"answer":"verified"}'}],
                "usage": {"input_tokens": 15, "input_tokens_details": {"cached_tokens": 5}, "output_tokens": 3},
            }
        elif provider == "anthropic":
            result = {
                "content": [{"type": "tool_use", "name": "respond", "input": {"answer": "verified"}}],
                "usage": {"input_tokens": 10, "cache_read_input_tokens": 5, "output_tokens": 3},
            }
        elif provider == "gemini":
            result = {
                "candidates": [{"content": {"parts": [{"text": '{"answer":"verified"}'}]}}],
                "usageMetadata": {
                    "promptTokenCount": 15,
                    "cachedContentTokenCount": 5,
                    "candidatesTokenCount": 2,
                    "thoughtsTokenCount": 1,
                },
            }
        else:
            result = {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [{"function": {"name": "respond", "arguments": '{"answer":"verified"}'}}]
                        }
                    }
                ],
                "usage": {"prompt_tokens": 15, "prompt_tokens_details": {"cached_tokens": 5}, "completion_tokens": 3},
            }
        return httpx.Response(200, json=result)

    monkeypatch.setattr(httpx, "request", request)
    value, usage = providers.invoke(provider, "private-test-key", model, Answer, "system", "question")
    answer = Answer.model_validate_json(value) if isinstance(value, str) else Answer.model_validate(value)
    assert answer.answer == "verified" and usage == {"input": 15, "cached": 5, "output": 3, "reported": True}
    assert captured[0][1].startswith(providers.BASES[provider])
    assert "private-test-key" not in str(captured[0][2]["json"])
    assert captured[0][2]["follow_redirects"] is False


def test_discovery_is_account_scoped_and_filters_incompatible_models(monkeypatch):
    providers._CACHE.clear()
    calls = []

    def request(provider, key, path, **kw):
        calls.append(key)
        return {
            "data": [
                {"id": "gpt-4.1" if key == "key-one" else "gpt-4o"},
                {"id": "text-embedding-3-small"},
                {"id": "gpt-image-1"},
                {"id": "gpt-4o-audio-preview"},
            ]
        }

    monkeypatch.setattr(providers, "_request", request)
    assert [m["id"] for m in providers.discover("openai", "key-one", "user-one")] == ["gpt-4.1"]
    assert [m["id"] for m in providers.discover("openai", "key-two", "user-two")] == ["gpt-4o"]
    providers.discover("openai", "key-one", "user-one")
    assert calls == ["key-one", "key-two"]


def test_managed_calls_reconcile_model_specific_prices_and_restore_totals(account, monkeypatch):
    store, user = account
    monkeypatch.setattr(get_settings(), "prices", {"openai:gpt-6.1-terra": (1, 0.1, 5)})
    monkeypatch.setattr(providers, "discover", lambda *a, **kw: [{"id": "gpt-6.1-terra"}])
    monkeypatch.setattr(
        providers,
        "invoke",
        lambda *a: ({"answer": "yes"}, {"input": 1000, "cached": 500, "output": 100, "reported": True}),
    )
    pin = store.pin_ai(user)
    llm = ChatLLM(selection=pin, store=store, run_id="priced-run")
    assert llm.ask("interpreter", Answer, "system", "question").answer == "yes"
    expected = (500 + 50 + 500) / 1e6
    assert store.usd_today(user["id"]) == pytest.approx(expected)
    restored = ChatLLM(selection=pin, store=store, run_id="priced-run")
    assert len(restored.tracker.calls) == 1 and restored.tracker.usd == pytest.approx(expected)


def test_failed_personal_provider_never_uses_shared_key(account, monkeypatch):
    store, user = account
    store.add_credential(user["id"], "groq", "personal-rejected-key", "llama-3.3-70b-versatile")
    seen = []

    def discover(provider, key, *a, **kw):
        seen.append((provider, key))
        raise providers.ProviderError("Rejected key", 401)

    monkeypatch.setattr(providers, "discover", discover)
    with pytest.raises(providers.ProviderError):
        ChatLLM(selection=store.pin_ai(user), store=store, run_id="r").ask("analyst", Answer, "s", "u")
    assert seen == [("groq", "personal-rejected-key")]


def test_unknown_shared_pricing_blocks_call_before_spending(account, monkeypatch):
    store, user = account
    store.set_ai_profile(user["id"], model="gpt-unknown-future")
    monkeypatch.setattr(providers, "discover", lambda *a, **kw: [{"id": "gpt-unknown-future"}])
    monkeypatch.setattr(providers, "invoke", lambda *a: pytest.fail("unpriced shared call"))
    with pytest.raises(providers.ProviderError, match="pricing"):
        ChatLLM(selection=store.pin_ai(user), store=store, run_id="r").ask("analyst", Answer, "s", "u")
    assert store.usd_today(BUDGET_KEY) == 0


@pytest.mark.parametrize('uncertain', [False, True])
def test_provider_failure_releases_only_confirmed_rejections(account, monkeypatch, uncertain):
    store, user = account
    pin = store.pin_ai(user)
    monkeypatch.setattr(providers, 'discover', lambda *a, **kw: [{'id': pin['model']}])
    def fail(*args):
        raise providers.ProviderError('Provider unavailable', 502, uncertain=uncertain)
    monkeypatch.setattr(providers, 'invoke', fail)
    with pytest.raises(providers.ProviderError):
        ChatLLM(selection=pin, store=store, run_id='failed').ask('interpreter', Answer, 's', 'u')
    row = store.api_call_history('failed', user['id'])[0]
    assert row['status'] == ('uncertain' if uncertain else 'rejected')
    assert store.usd_today(user['id']) == (row['reserved_usd'] if uncertain else 0)
    restored = ChatLLM(selection=pin, store=store, run_id='failed')
    assert restored.tracker.usd == pytest.approx(round(store.usd_today(user['id']), 5))


def test_invalid_output_is_charged_and_provider_errors_are_redacted(account, monkeypatch):
    store, user = account
    pin = store.pin_ai(user)
    monkeypatch.setattr(providers, 'discover', lambda *a, **kw: [{'id': pin['model']}])
    monkeypatch.setattr(providers, 'invoke', lambda *a: ('invalid-json',
                       {'input': 100, 'cached': 0, 'output': 50, 'reported': True}))
    with pytest.raises(providers.ProviderError, match='invalid analytical structure'):
        ChatLLM(selection=pin, store=store, run_id='invalid').ask('analyst', Answer, 's', 'u')
    assert store.usd_today(user['id']) > 0
    monkeypatch.setattr(httpx, 'request', lambda *a, **kw: httpx.Response(401, text='private-key-secret'))
    with pytest.raises(providers.ProviderError) as error:
        providers._request('groq', 'private-key-secret', '/models')
    assert 'private-key-secret' not in str(error.value)
    with pytest.raises(providers.ProviderError, match='format'):
        providers._request('openai', 'key\r\ninjection', '/models')


def test_invalid_pricing_cannot_credit_budget(account, monkeypatch):
    monkeypatch.setattr(get_settings(), 'prices', {'openai:bad': (-1, 0, 2)})
    with pytest.raises(providers.ProviderError, match='pricing'):
        providers.rates('openai', 'bad')


@pytest.mark.parametrize('provider', ['openai', 'anthropic', 'gemini', 'groq'])
def test_partial_usage_is_not_mistaken_for_a_free_call(provider, monkeypatch):
    monkeypatch.setattr(httpx, 'request', lambda *a, **kw: httpx.Response(200, json={
        'usage': {'input_tokens': 20}, 'usageMetadata': {'promptTokenCount': 20}}))
    _, usage = providers.invoke(provider, 'local-test-key', 'model', Answer, 's', 'u')
    assert usage['reported'] is False
