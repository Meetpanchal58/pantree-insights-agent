"""LLM access: one small interface for every agent, with cost tracking and a daily budget.

Agents call `llm.ask(agent, Schema, system, user)` and get a validated pydantic object back. The real
implementation uses LangChain's init_chat_model, so switching provider (OpenAI now, Anthropic later)
is a settings change. Tests use FakeLLM, which never touches the network.
"""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from .config import ROOT, get_settings

T = TypeVar("T", bound=BaseModel)
SPEND_FILE = ROOT / ".cache" / "llm_spend.json"


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Call:
    agent: str
    model: str
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    usd: float
    seconds: float
    queue_seconds: float = 0.0
    inference_seconds: float = 0.0


@dataclass
class CostTracker:
    """Everything one question cost."""
    calls: list[Call] = field(default_factory=list)

    @property
    def usd(self) -> float:
        return round(sum(c.usd for c in self.calls), 5)

    @property
    def tokens(self) -> dict[str, int]:
        return {"input": sum(c.input_tokens for c in self.calls), "cached": sum(c.cached_tokens for c in self.calls),
                "output": sum(c.output_tokens for c in self.calls)}

    def summary(self) -> dict[str, Any]:
        return {"usd": self.usd, "calls": len(self.calls), **self.tokens}


def price(model: str, input_tokens: int, cached: int, output: int) -> float:
    name = model.split(":", 1)[-1]
    rate_in, rate_cached, rate_out = get_settings().prices.get(name, (0.0, 0.0, 0.0))
    return ((input_tokens - cached) * rate_in + cached * rate_cached + output * rate_out) / 1e6


BUDGET_KEY = "__daily_llm_budget__"     # one row per day in the usage table, for all users together


def spent_today() -> float:
    """Today's LLM spend. Kept in the app database (Postgres in production) so restarts and cold starts
    do not reset it; falls back to a local file when no database is reachable (e.g. CLI without setup)."""
    try:
        from .store import get_store
        return get_store().usd_today(BUDGET_KEY)
    except Exception:
        pass
    try:
        return json.loads(SPEND_FILE.read_text()).get(str(date.today()), 0.0)
    except (OSError, ValueError):
        return 0.0


def _record_spend(usd: float) -> None:
    try:
        from .store import get_store
        get_store().add_usd_today(BUDGET_KEY, usd)
        return
    except Exception:
        pass
    try:
        data = json.loads(SPEND_FILE.read_text())
    except (OSError, ValueError):
        data = {}
    today = str(date.today())
    data = {today: round(data.get(today, 0.0) + usd, 6)}          # keep only today
    SPEND_FILE.parent.mkdir(parents=True, exist_ok=True)
    SPEND_FILE.write_text(json.dumps(data))


class LLM(Protocol):
    tracker: CostTracker

    def ask(self, agent: str, schema: type[T], system: str, user: str, *, effort: str | None = None,
            light: bool = False) -> T: ...


_slots_lock = threading.Lock()
_slots: dict[int, threading.BoundedSemaphore] = {}


@lru_cache(maxsize=16)
def _shared_model(name: str, effort: str, max_tokens: int, timeout: float, retries: int, temperature: float | None):
    """Reuse provider connection pools across requests; usage trackers remain private to each run."""
    from langchain.chat_models import init_chat_model

    kwargs: dict[str, Any] = {"max_tokens": max_tokens, "timeout": timeout, "max_retries": retries}
    if temperature is not None:
        kwargs["temperature"] = temperature
    if name.startswith("openai:") and effort != "none":
        kwargs["reasoning"] = {"effort": effort}
    return init_chat_model(name, **kwargs)


def _provider_slots(limit: int) -> threading.BoundedSemaphore:
    """Prevent concurrent agent runs from stampeding the shared API quota."""
    with _slots_lock:
        if limit not in _slots:
            _slots[limit] = threading.BoundedSemaphore(max(1, limit))
        return _slots[limit]


class ChatLLM:
    """Real models through LangChain (structured output, reasoning effort, usage tracking)."""

    def __init__(self, tracker: CostTracker | None = None, main: str | None = None, light: str | None = None):
        settings = get_settings()
        self.tracker = tracker or CostTracker()
        self.main = main or settings.model_main
        self.light = light or settings.model_light

    def _model(self, name: str, effort: str):
        settings = get_settings()
        return _shared_model(name, effort, settings.max_output_tokens, settings.llm_timeout_s,
                             settings.llm_max_retries, settings.temperature)

    def ask(self, agent: str, schema: type[T], system: str, user: str, *, effort: str | None = None,
            light: bool = False) -> T:
        settings = get_settings()
        if spent_today() >= settings.daily_budget_usd:
            raise BudgetExceeded(f"Today's LLM budget (${settings.daily_budget_usd:.2f}) is used up.")
        name = self.light if light else self.main
        model = self._model(name, effort or settings.reasoning_effort)
        runnable = model.with_structured_output(schema, include_raw=True)
        started = time.perf_counter()
        gate = _provider_slots(settings.max_parallel_llm_calls)
        # Keep model calls bounded across users and chats on this Cloud Run instance.
        # This does not override provider RPM/TPM quotas.
        if not gate.acquire(timeout=settings.llm_timeout_s):
            raise TimeoutError("Model request queue is busy; retry after the current run finishes")
        inference_started = time.perf_counter()
        try:
            result = runnable.invoke([("system", system), ("user", user)])
        finally:
            gate.release()
        usage = getattr(result["raw"], "usage_metadata", None) or {}
        cached = (usage.get("input_token_details") or {}).get("cache_read", 0) or 0
        call = Call(agent, name, usage.get("input_tokens", 0), cached, usage.get("output_tokens", 0), 0.0,
                    round(time.perf_counter() - started, 2))
        call.queue_seconds = round(inference_started - started, 6)
        call.inference_seconds = round(time.perf_counter() - inference_started, 6)
        call.usd = round(price(name, call.input_tokens, cached, call.output_tokens), 6)
        self.tracker.calls.append(call)
        _record_spend(call.usd)
        if result.get("parsing_error") or result.get("parsed") is None:
            raise ValueError(f"{agent}: model output did not match {schema.__name__}: {result.get('parsing_error')}")
        return result["parsed"]


class FakeLLM:
    """For tests: `responder(agent, schema, system, user)` returns the schema object (or a dict for it)."""

    def __init__(self, responder: Callable[[str, type[BaseModel], str, str], Any]):
        self.responder = responder
        self.tracker = CostTracker()
        self.prompts: list[tuple[str, str, str]] = []      # (agent, system, user) for assertions

    def ask(self, agent: str, schema: type[T], system: str, user: str, *, effort: str | None = None,
            light: bool = False) -> T:
        self.prompts.append((agent, system, user))
        out = self.responder(agent, schema, system, user)
        self.tracker.calls.append(Call(agent, "fake", len(system + user) // 4, 0, 50, 0.0, 0.0))
        return out if isinstance(out, schema) else schema.model_validate(out)
