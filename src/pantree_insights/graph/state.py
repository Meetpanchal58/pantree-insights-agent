"""Graph: state."""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, TypedDict

import pandas as pd
from langgraph.config import get_stream_writer

MAX_CLARIFY_ROUNDS = 6        # stop without computing if material questions remain unresolved


CONFIRM_QUESTION = "Is this what you want me to compute?"


CONFIRM_YES, CONFIRM_NO = "Yes, go ahead", "No, change something"


CORRECTION = "Correction to your reading of the question"


CACHE_SIZE = 256


log = logging.getLogger("pantree.graph")


@dataclass
class OpenPoint:
    """A question for the user. `meaning` maps an option to what code does with it (internal)."""
    key: str
    question: str
    options: list[str]
    default: str
    why: str
    meaning: dict[str, str] = field(default_factory=dict)
    required: bool = False                     # no default may answer it, even when defaults were requested

    def public(self) -> dict:
        return {"question": self.question, "options": self.options, "default": self.default, "why": self.why}


class State(TypedDict, total=False):
    run_id: str
    chat_id: str | None
    question: str
    history: str
    relationship: str
    objective: str
    previous_spec: dict | None
    plan_version: int
    plan_digest: str
    confirmed_digest: str | None
    prepared: list[dict]
    revision: str
    investigation: dict
    results: list[dict]
    adaptive: bool
    investigation_round: int
    completed_results: list[dict]
    completed_analyses: list[dict]
    probe_ledger: list[dict]
    investigation_stop: str
    analyses: list[dict]
    answers: dict[str, str]
    choices: dict[str, str]
    open_points: list[dict]
    clarify_rounds: int
    clarification_notice: str
    fast_path: bool
    readings: list[dict]                       # different readings offered to the user
    spec: dict | None                          # the confirmed QuerySpec
    template: dict | None                      # or the confirmed template call
    restated: str
    readback: str
    notes: list[str]                           # assumptions and premise facts the answer must state
    trust: str | None
    memory: str
    metric_names: list[str]
    sql: str
    result: dict                               # {"columns": [...], "data": [[...]], "truncated": bool}
    verification: dict
    warnings: list[str]
    analysis: dict
    grounded: bool
    chart: dict | None
    insight_id: str | None
    status: str                                # running | declined | failed | done
    message: str
    route: str


def _frame(result: dict) -> pd.DataFrame:
    return pd.DataFrame(result["data"], columns=result["columns"])


def _result(frame: pd.DataFrame, truncated: bool) -> dict:
    split = json.loads(frame.to_json(orient="split", date_format="iso", index=False, double_precision=15))
    return {"columns": split["columns"], "data": split["data"], "truncated": truncated}


def _emit(step: str, status: str, **detail: Any) -> None:
    with contextlib.suppress(RuntimeError):      # outside a streaming run (e.g. unit tests)
        get_stream_writer()({"type": "step", "step": step, "status": status, "detail": detail, "t": time.time()})


def step(name: str):
    """Emit started / done (with duration) around a node; failures are reported, then re-raised."""
    def wrap(fn: Callable[..., dict]):
        @wraps(fn)
        def node(self, state: State) -> dict:
            _emit(name, "started")
            started = time.perf_counter()
            try:
                update = fn(self, state)
            except Exception as error:
                _emit(name, "failed", error=str(error)[:300])
                raise
            detail = update.pop("_detail", {}) if isinstance(update, dict) else {}
            _emit(name, update.get("_status", "done") if isinstance(update, dict) else "done",
                  seconds=round(time.perf_counter() - started, 6), **detail)
            if isinstance(update, dict):
                update.pop("_status", None)
            return update
        return node
    return wrap


def plan_digest(prepared: list[dict], version: int, snapshot: str | None) -> str:
    return hashlib.sha256(json.dumps([prepared, version, snapshot], sort_keys=True).encode()).hexdigest()
