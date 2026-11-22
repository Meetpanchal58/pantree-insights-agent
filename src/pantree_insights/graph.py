"""The agent as a LangGraph state machine. The model decides which question is answered, never which number.

    check_input -> interpret -> clarify (questions or read-back; pauses for the user) -> interpret ...
                   interpret -> execute (compiled spec or reviewed template; no model writes SQL)
                   execute -> analyse (numbers rendered by code) -> chart -> remember -> finish

Every step streams progress events ({"type": "step", "step", "status", "detail", ...}). A run pauses at a question
or the read-back and resumes with Agent.resume(). See docs/ARCHITECTURE.md.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, TypedDict

import pandas as pd
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from . import agents
from .catalog import label_columns, load_catalog
from .charts import build_chart
from .config import get_settings
from .context import populations
from .db import connect, manifest, run_query, snapshot_signature
from .fastpath import followup, independent_history
from .fastpath import parse as fast_spec
from .grounding import Claim, KeyValue, free_numbers, unsupported_assertion, verify
from .guardrails import check_question, check_sql
from .interpret import READINGS, Decision, TemplateCall, canonical, decide, readback
from .joins import JoinGraph
from .llm import LLM, BudgetExceeded, ChatLLM
from .memory import Memory
from .profile import profile_for
from .semantic import load_concepts, load_glossary, load_metrics, load_schema
from .spec import Catalog, QuerySpec, SpecError, compile_spec, validate
from .templates import RESULT_UNITS, TemplateError
from .templates import build as build_template

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


def drop_unverified(analysis: agents.Analysis, problems: list[str]) -> agents.Analysis:
    """Remove every sentence that uses a failing claim or a free-typed number; keep the rest."""
    bad = {p.split(" ", 1)[0] for p in problems if re.match(r"c\d+ ", p)}
    claims = [c for c in analysis.claims if c.id not in bad]

    def clean(text: str) -> bool:
        used = set(re.findall(r"\{(c\d+)\}", text or ""))
        return (not (used & bad) and not free_numbers(text or "") and used <= {c.id for c in claims}
                and not unsupported_assertion(text or ""))

    headline = analysis.headline if clean(analysis.headline) else \
        "The verified result is in the table below; no number in the summary could be checked."
    return analysis.model_copy(update={
        "headline": headline, "claims": claims,
        "findings": [t for t in analysis.findings if clean(t)],
        "hypotheses": [t for t in analysis.hypotheses if clean(t)],
        "recommendations": [t for t in analysis.recommendations if clean(t)],
        "caveats": [t for t in analysis.caveats if clean(t)],
        "chart_title": analysis.chart_title if clean(analysis.chart_title) else "Verified result",
        "chart_subtitle": analysis.chart_subtitle if clean(analysis.chart_subtitle) else "",
        "follow_up_questions": [t for t in analysis.follow_up_questions if clean(t)],
        "memory": [m for m in analysis.memory if clean(m.statement) and re.search(r"\{c\d+\}", m.statement)]})


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


class Agent:
    _definition_version: str | None = None
    _definition_lock = threading.Lock()

    def __init__(self, llm_factory: Callable[[], LLM] | None = None, memory: Memory | None = None, con=None,
                 data: dict | None = None):
        settings = get_settings()
        self.llm_factory = llm_factory or ChatLLM
        started = time.monotonic()
        log.info("agent_prepare phase=duckdb started")
        self._owns_connection = con is None
        self.con = con or connect(settings.shop_dir)
        log.info("agent_prepare phase=duckdb seconds=%.2f", time.monotonic() - started)
        self.memory = memory or Memory()
        self.snapshot = snapshot_signature() if data is None else None
        with self._definition_lock:
            if self.snapshot != Agent._definition_version:
                for loader in (load_metrics, load_glossary, load_schema, load_concepts, load_catalog):
                    loader.cache_clear()
                agents.prompts.cache_clear()
                Agent._definition_version = self.snapshot
            self.metrics, self.glossary = load_metrics(), load_glossary()
        info = data or manifest(settings.shop_dir)
        self.data = {"first_day": info["first_day"], "last_day": info["last_day"], "size": info["size"]}
        schema = load_schema()
        started = time.monotonic()
        log.info("agent_prepare phase=joins data_size=%s data_as_of=%s started", info["size"], info["last_day"])
        profile = profile_for(self.con, {**info, "snapshot_signature": self.snapshot}, joins_only=True)
        self.catalog = Catalog(schema, self.metrics, load_concepts(), JoinGraph(schema, profile), load_catalog(),
                               label_columns(), self.con)
        log.info("agent_prepare phase=joins seconds=%.2f", time.monotonic() - started)
        self.aliases = {alias.lower(): value for value, names in self.glossary.value_aliases.items()
                        for alias in names}
        self._cache: OrderedDict[str, tuple[pd.DataFrame, bool]] = OrderedDict()
        self._cache_lock = threading.Lock()
        self._llms: dict[str, LLM] = {}
        self._memories: dict[str, Any] = {}          # run_id -> memory scope for that question
        self._checkpoint_context = None
        started = time.monotonic()
        log.info("agent_prepare phase=checkpoint backend=%s started", settings.checkpoint_backend)
        if settings.checkpoint_backend == "postgres":
            from langgraph.checkpoint.postgres import PostgresSaver
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool

            dsn = settings.database_url.replace("postgresql+psycopg://", "postgresql://")
            if not dsn.startswith(("postgresql://", "postgres://")):
                raise RuntimeError("CHECKPOINT_BACKEND=postgres requires a PostgreSQL DATABASE_URL")
            self._checkpoint_context = ConnectionPool(dsn, min_size=1, max_size=4, timeout=15,
                kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}, open=False)
            self._checkpoint_context.__enter__()
            saver = PostgresSaver(self._checkpoint_context)
            try:
                saver.setup()
            except Exception:
                self.close()
                raise
        else:
            saver = InMemorySaver()
        self._saver = saver
        self.graph = self._build().compile(checkpointer=saver)
        log.info("agent_prepare phase=checkpoint seconds=%.2f", time.monotonic() - started)

    def restore_memory(self, run_id: str, scope: Any) -> None:
        """Reattach user/project memory to a paused persistent graph after a restart."""
        self._memories[run_id] = scope

    def close(self) -> None:
        if self._checkpoint_context is not None:
            self._checkpoint_context.__exit__(None, None, None)
            self._checkpoint_context = None
        if self._owns_connection:
            self.con.close()
            self._owns_connection = False

    def llm(self, state: State) -> LLM:
        if state["run_id"] not in self._llms:
            self._llms[state["run_id"]] = self.llm_factory()
        return self._llms[state["run_id"]]

    def mem(self, state: State):
        return self._memories.get(state["run_id"], self.memory)

    def query(self, sql: str) -> tuple[pd.DataFrame, bool]:
        """Run verified SQL, cached by (SQL, data snapshot): a repeat question is exact and instant."""
        key = f"{self.snapshot}|{sql}"
        with self._cache_lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        result = run_query(self.con, sql)
        with self._cache_lock:
            self._cache[key] = (result.frame, result.truncated)
            while len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)
        return result.frame, result.truncated

    # ------------------------------------------------------------------ nodes
    @step("check_input")
    def check_input(self, state: State) -> dict:
        verdict = check_question(state["question"])
        if not verdict.ok:
            return {"status": "declined", "message": verdict.reason, "route": "finish", "_status": "blocked",
                    "_detail": {"reason": verdict.reason}}
        return {"status": "running", "route": "interpret", "warnings": []}

    @step("interpret")
    def interpret(self, state: State) -> dict:
        """Question -> spec or template, with every doubt turned into a question and a read-back to confirm."""
        answers, choices = dict(state.get("answers") or {}), dict(state.get("choices") or {})
        rounds = state.get("clarify_rounds", 0)
        background = state.get("memory")
        previous = QuerySpec.model_validate(state["previous_spec"]) if state.get("previous_spec") else None
        context_started = time.perf_counter()
        reviewed = fast_spec(state["question"], self.catalog, self.data["last_day"]) or followup(
            state["question"], previous, self.catalog, self.data["last_day"])
        if background is None:
            scope = self.mem(state)
            constraints = scope.constraints() if hasattr(scope, "constraints") else None
            skip_context = constraints == "" and reviewed is not None and not answers and independent_history(
                state.get("history", ""), self.catalog, self.data["last_day"])
            background = "" if skip_context else scope.context(state["question"], as_of=self.data["last_day"])
        needs_evidence = not (background == "" and reviewed is not None and independent_history(
            state.get("history", ""), self.catalog, self.data["last_day"]))
        if needs_evidence and state.get("investigation") and "## Investigation" not in background:
            background += "\n## Investigation (verified historical evidence; recheck against current data)\n" + \
                json.dumps({k: v for k, v in state["investigation"].items()
                            if k not in ("populations", "historical_populations")}, default=str)
        _emit("context", "done", seconds=round(time.perf_counter() - context_started, 6))
        pick = choices.pop("reading", None)
        revision = state.get("revision")
        revised_items = None
        if revision and (state.get("spec") or state.get("template")):
            from .interpret import revise, revise_template
            items = state.get("prepared") or [{"spec": state.get("spec"), "template": state.get("template"),
                                                 "purpose": state.get("restated", "Analysis")}]
            target = re.fullmatch(r"step ([1-4])[: ,]+(.+)", revision, re.I)
            if len(items) > 1 and (not target or int(target[1]) > len(items)):
                decision = Decision("clarify", required=True, questions=[agents.ClarifyingQuestion(
                    question="Which part should change? Say 'step 1: ...' and describe the correction.",
                    options=[], default="", why="The other approved requirements will be preserved")])
            else:
                index, correction = (int(target[1]) - 1, target[2]) if target else (0, revision)
                try:
                    if items[index].get("spec"):
                        decision = revise(self.llm(state), self.catalog, QuerySpec.model_validate(items[index]["spec"]),
                                          correction, self.data)
                    else:
                        decision = revise_template(self.llm(state), self.catalog,
                            TemplateCall.model_validate(items[index]["template"]), correction, self.data)
                    if (decision.spec or decision.template) and len(items) > 1:
                        revised_items = [dict(item) for item in items]
                        revised_items[index] = {**items[index],
                            "spec": decision.spec.model_dump(mode="json") if decision.spec else None,
                            "template": decision.template.model_dump(mode="json") if decision.template else None}
                except (SpecError, TemplateError, ValueError) as error:
                    decision = Decision("clarify", required=True, questions=[agents.ClarifyingQuestion(
                        question=f"I couldn't preserve that scope: {error}. What should I change?",
                        options=[], default="", why="The existing plan remains unexecuted")])
        elif pick is not None and state.get("readings"):           # the user chose one of the readings
            spec = QuerySpec.model_validate(state["readings"][int(pick)])
            decision = Decision("spec", spec=spec, restated=state.get("restated", state["question"]))
        else:
            decision = decide(self.llm(state), self.catalog, self.glossary.terms, self.aliases, self.data,
                              state["question"], state.get("history", ""), answers, self.glossary.ambiguous,
                              contrast=get_settings().contrast_check, background=background, previous=previous,
                              investigation=state.get("investigation"), snapshot=self.snapshot)
        detail = {"decision": decision.kind, "restated": decision.restated, "reason": decision.reason,
                  "fast_path": bool(decision.detail.get("fast_path"))}
        if decision.kind == "failed":
            return {"status": "failed", "message": decision.reason, "route": "finish",
                    "open_points": [], "confirmed_digest": None, "_detail": {**detail, **decision.detail}}
        if decision.kind == "decline":
            return {"status": "declined", "message": decision.reason, "route": "finish", "_status": "declined",
                    "_detail": {**detail, "decision": "decline"}}
        if decision.kind == "unanswerable":
            message = f"I can't answer this as a verified number yet. {decision.reason}"
            return {"status": "declined", "message": message, "route": "finish", "_detail": detail}
        if decision.kind == "clarify":
            if rounds >= MAX_CLARIFY_ROUNDS:
                return {"status": "declined", "route": "finish", "open_points": [],
                        "message": "The question is still unclear. Please restate the metric, period and filters; "
                                   "nothing was computed from guessed values."}
            points = []
            for q in decision.questions:
                if q.question.startswith(READINGS):
                    meaning = {o: str(i) for i, o in enumerate(q.options)}
                    points.append(OpenPoint("reading", q.question, q.options, q.default, q.why, meaning, True))
                else:
                    points.append(OpenPoint(f"q:{q.question}", q.question, q.options, q.default, q.why,
                                            required=decision.required))
            done = {a.strip().lower() for a in answers}
            pending = [p for p in points if p.question.strip().lower() not in done]
            if not pending:
                return {"status": "failed", "message": "I could not apply your answer to the plan. "
                        "Your earlier results are saved; no new analysis was executed.",
                        "route": "finish", "open_points": [], "confirmed_digest": None, "_detail": detail}
            return {"open_points": [p.__dict__ for p in pending], "route": "clarify", "_status": "needs_input",
                    "confirmed_digest": None,
                    "memory": background,
                    "readings": [r.model_dump(mode="json") for r in decision.readings],
                    "restated": decision.restated,
                    "_detail": {**detail, "round": rounds + 1, "questions": [p.question for p in pending]}}
        if decision.kind == "investigation":
            items = [step.model_dump(mode="json") for step in decision.steps]
            summary = "\n\n".join(f"{i + 1}. {item['purpose']}\n" + (
                readback(QuerySpec.model_validate(item["spec"]), self.catalog) if item.get("spec") else
                build_template(item["template"]["name"], TemplateCall.model_validate(item["template"]).params)[1])
                for i, item in enumerate(items))
            out = {"spec": items[0].get("spec"), "template": items[0].get("template"), "prepared": items}
        elif decision.kind == "template":
            _, summary = build_template(decision.template.name, decision.template.params)
            summary = f"**Analysis:** {summary}" + (f"\n**Assumed:** {'; '.join(decision.assumptions)}"
                                                     if decision.assumptions else "")
            out = {"template": {"name": decision.template.name, "params": decision.template.params}, "spec": None}
        else:
            summary = readback(decision.spec, self.catalog, decision.assumptions)
            out = {"spec": decision.spec.model_dump(mode="json"), "template": None}
        if revised_items:
            summary = "\n\n".join(f"{i + 1}. {item['purpose']}\n" + (
                readback(QuerySpec.model_validate(item["spec"]), self.catalog) if item.get("spec") else
                build_template(item["template"]["name"], TemplateCall.model_validate(item["template"]).params)[1])
                for i, item in enumerate(revised_items))
            out = {"spec": revised_items[0].get("spec"), "template": revised_items[0].get("template"),
                   "prepared": revised_items}
        relationship = decision.detail.get("relationship", "refinement" if revision else
            "drilldown" if previous and followup(state["question"], previous, self.catalog, self.data["last_day"])
            else "new")
        objective = ((state.get("investigation") or {}).get("objective") if relationship != "new" else None)
        out.update({"relationship": relationship, "objective": objective or state["question"], "readback": summary,
                    "restated": decision.restated, "notes": decision.assumptions,
                    "trust": None, "open_points": [], "answers": answers, "choices": choices,
                    "memory": background, "revision": "",
                    "adaptive": state.get("adaptive", False) or bool(decision.detail.get("adaptive")),
                    "fast_path": bool(decision.detail.get("fast_path")),
                    "_detail": {**detail, "readback": summary}})
        if out.get("adaptive"):
            out["readback"] += ("\n\n**Investigation:** After these results I may propose further checks. "
                                "Each needs your approval. This request stops after three rounds or eight queries.")
        try:
            prepared = self.prepare(out)
        except (SpecError, TemplateError) as error:
            point = OpenPoint("scope", "I need to adjust the scope: " + str(error) + ". What would you like to change?",
                              [], "", "The unavailable part cannot be treated as zero", required=True)
            return {**out, "open_points": [point.__dict__], "prepared": out.get("prepared", []),
                    "confirmed_digest": None,
                    "route": "clarify", "_status": "needs_input"}
        version = state.get("plan_version", 0) + 1
        point = OpenPoint("confirm", CONFIRM_QUESTION, [], "", "Please confirm or tell me what to change")
        return {**out, "plan_version": version, "prepared": prepared,
                "plan_digest": plan_digest(prepared, version, self.snapshot), "confirmed_digest": None,
                "open_points": [point.__dict__], "route": "clarify", "_status": "needs_input"}

    def prepare(self, state: State) -> list[dict]:
        """Compile and check scope before confirmation; never execute analytical SQL here."""
        from .interpret import coverage_problems
        items = state.get("prepared") or [{"spec": state.get("spec"), "template": state.get("template"),
                                            "purpose": state.get("restated", "Requested analysis")}]
        prepared = []
        for item in items:
            spec = QuerySpec.model_validate(item["spec"]) if item.get("spec") else None
            call = TemplateCall.model_validate(item["template"]) if item.get("template") else None
            problems = coverage_problems(spec, call, self.data)
            if problems:
                raise SpecError("; ".join(problems))
            if spec:
                problems = validate(spec, self.catalog)
                if problems:
                    raise SpecError("; ".join(problems))
                sql = compile_spec(spec, self.catalog)
            elif call:
                sql, _ = build_template(call.name, call.params)
            else:
                raise SpecError("No analytical plan was supplied")
            safe = check_sql(sql)
            if not safe.ok:
                raise SpecError("the proposed analysis failed SQL safety checks")
            prepared.append({"purpose": item.get("purpose", "Requested analysis"),
                             "spec": spec.model_dump(mode="json") if spec else None,
                             "template": call.model_dump(mode="json") if call else None, "sql": safe.sql})
        if not 1 <= len(prepared) <= 4:
            raise SpecError("An investigation must contain one to four queries")
        return prepared

    def clarify(self, state: State) -> dict:
        """A conversational interrupt: only a positive reply to this exact plan can approve execution."""
        from .interpret import is_confirmation
        points = [OpenPoint(**p) for p in state.get("open_points") or []]
        confirming = bool(state.get("prepared")) and any(p.key == "confirm" for p in points)
        version = state.get("plan_version", 0)
        text = ("I understand you want to " + (state.get("restated") or state["question"]).rstrip(".?") +
                ".\n\n" + state.get("readback", "") +
                "\n\nDoes this match your objective? Say ‘go ahead’ to run this plan, or tell me what to change.") \
            if confirming else "\n\n".join(p.question + (" Possible meanings: " + "; ".join(p.options)
                                                          if p.options else "") for p in points)
        payload = {"type": "clarify", "kind": "confirm" if confirming else "clarify", "message": text,
                   "questions": [p.public() for p in points], "plan_version": version,
                   "readback": state.get("readback"), "spec": state.get("spec"),
                   "template": state.get("template"), "plan": state.get("prepared", [])}
        if not text.strip():
            return {"status": "failed",
                    "message": "The planner returned no question or plan. Please retry this request.",
                    "route": "finish", "confirmed_digest": None}
        given = interrupt(payload)
        message = str(given.get("message", "")).strip() if isinstance(given, dict) else ""
        if re.fullmatch(r"(?:please )?(?:cancel|stop|never mind|nevermind)(?: (?:it|this|the analysis))?[.!]?",
                        message, re.I):
            return {"status": "cancelled", "message": "Cancelled. I haven't run this pending plan.",
                    "confirmed_digest": None, "route": "finish"}
        verdict = check_question(message)
        if not verdict.ok:
            return {"status": "declined", "message": verdict.reason, "confirmed_digest": None, "route": "finish"}
        if not message or given.get("plan_version") != version:
            return {"route": "clarify", "confirmed_digest": None}
        if confirming and is_confirmation(message):
            return {"confirmed_digest": state["plan_digest"], "open_points": [], "route": "execute"}
        if is_confirmation(message):
            return {"route": "clarify", "confirmed_digest": None}  # an unanswered question is not a plan
        if state.get("clarify_rounds", 0) >= MAX_CLARIFY_ROUNDS:
            return {"status": "declined", "message": "Let's restart with a concise objective and scope.",
                    "route": "finish"}
        if confirming:
            return {"revision": message, "confirmed_digest": None, "route": "interpret",
                    "clarify_rounds": state.get("clarify_rounds", 0) + 1}
        if len(points) == 1 and points[0].key == "reading":
            point = points[0]
            selected = next((value for option, value in point.meaning.items()
                             if message.casefold() == option.casefold()), None)
            if selected is None and re.fullmatch(r"(?:option )?[1-3]", message, re.I):
                index = int(message.split()[-1]) - 1
                selected = str(index) if index < len(point.options) else None
            if selected is not None:
                return {"choices": {"reading": selected}, "revision": "", "confirmed_digest": None,
                        "route": "interpret", "clarify_rounds": state.get("clarify_rounds", 0) + 1}
        answers = dict(state.get("answers") or {})
        if len(points) == 1:
            answers[points[0].question] = message
        else:
            answers[CORRECTION] = message
        return {"answers": answers, "revision": message if state.get("spec") or state.get("template") else "",
                "confirmed_digest": None, "route": "interpret",
                "clarify_rounds": state.get("clarify_rounds", 0) + 1}

    @step("execute")
    def execute(self, state: State) -> dict:
        """Compile the confirmed spec (or build the template) and run it. No model writes this SQL."""
        prepared = state.get("prepared") or []
        digest = plan_digest(prepared, state.get("plan_version", 0), self.snapshot)
        if (not prepared or state.get("confirmed_digest") != digest or state.get("plan_digest") != digest
                or (self.snapshot and snapshot_signature() != self.snapshot)):
            return {"status": "failed", "route": "finish",
                    "message": "This exact plan needs explicit confirmation before execution."}
        if state.get("adaptive") and state.get("completed_results"):
            from .investigate import MAX_QUERIES
            completed = state["completed_results"]
            if len(completed) + len(prepared) > MAX_QUERIES or any(
                    p["sql"] == old["sql"] for p in prepared for old in completed):
                return {"status": "failed", "route": "finish",
                        "message": "The revised probe repeats evidence or exceeds the investigation query budget."}
        started = time.perf_counter()
        results = []
        verification = {"status": "verified", "how": "executed the exact confirmed compiled plan",
                        "scope": "query and numeric evidence; descriptive results do not establish causes"}
        try:
            for i, item in enumerate(prepared):
                if self.snapshot and snapshot_signature() != self.snapshot:
                    raise SpecError("the data changed during execution; please ask again")
                frame, truncated = self.query(item["sql"])
                if self.snapshot and snapshot_signature() != self.snapshot:
                    raise SpecError("the data changed during execution; please ask again")
                result = _result(frame, truncated)
                results.append({**item, "result": result, "metric_names": [m["metric"] for m in
                    (item.get("spec") or {}).get("measures", []) if m.get("metric")]})
                with contextlib.suppress(RuntimeError):
                    get_stream_writer()({"type": "result", "run_id": state["run_id"], "query_index": i,
                        "purpose": item["purpose"], "result": result, "sql": item["sql"],
                        "readback": state.get("readback"), "trust": "verified", "verification": verification,
                        "data_as_of": self.data["last_day"], "data_size": self.data["size"],
                        "warnings": state.get("notes", [])})
        except (SpecError, TemplateError) as error:
            return {"status": "failed", "route": "finish", "results": results,
                    "message": f"The investigation stopped: {error}. Earlier streamed tables are partial evidence."}
        first = results[0]
        return {"sql": first["sql"], "metric_names": first["metric_names"], "result": first["result"],
                "results": results, "trust": "verified", "verification": verification, "route": "analyse",
                "_detail": {"queries": len(results), "query_seconds": round(time.perf_counter() - started, 6)}}

    @step("analyse")
    def analyse(self, state: State) -> dict:
        analyses = []
        for item in state.get("results") or [{"spec": state.get("spec"), "template": state.get("template"),
                                             "sql": state["sql"], "result": state["result"],
                                             "metric_names": state.get("metric_names", [])}]:
            answer = self._analyse_one({**state, **item})
            analyses.append({**item, "analysis": answer["analysis"], "grounded": answer["grounded"],
                             "warnings": answer["warnings"]})
        return {"analyses": analyses, "analysis": analyses[0]["analysis"],
                "grounded": all(a["grounded"] for a in analyses),
                "warnings": list(dict.fromkeys(w for a in analyses for w in a["warnings"])), "route": "chart"}

    @step("reflect")
    def reflect(self, state: State) -> dict:
        from .investigate import MAX_CALLS, MAX_QUERIES, MAX_ROUNDS, MAX_USD, resolve_probe, retain_scope, review
        if not state.get("adaptive"):
            return {"route": "chart"}
        results = [*state.get("completed_results", []), *state.get("results", [])]
        analyses = [*state.get("completed_analyses", []), *state.get("analyses", [])]
        round_number = state.get("investigation_round", 0) + 1
        ledger = list(state.get("probe_ledger", []))
        base = {"completed_results": results, "completed_analyses": analyses,
                "investigation_round": round_number, "probe_ledger": ledger}

        def stop(reason):
            summary = dict(analyses[0]["analysis"])
            if len(analyses) > 1:
                summary.update(headline=f"Investigation completed with {len(analyses)} approved queries.",
                    findings=[f"Query {i + 1}: {a['analysis']['headline']}" for i, a in enumerate(analyses)],
                    caveats=["Each finding refers to its own approved scope. "
                             "Observed associations do not prove causes."],
                    follow_up_questions=analyses[-1]["analysis"].get("follow_up_questions", []), memory=[])
            return {**base, "route": "chart", "analyses": analyses, "analysis": summary,
                    "grounded": all(a["grounded"] for a in analyses), "investigation_stop": reason}

        if not state.get("grounded") or any(r["result"].get("truncated") for r in results):
            return stop("Evidence is incomplete or unverified; further probes need a new scope.")
        tracker = self.llm(state).tracker
        if round_number >= MAX_ROUNDS or len(results) >= MAX_QUERIES:
            return stop("Investigation query/round limit reached. You can continue in another request.")
        if len(tracker.calls) >= MAX_CALLS or tracker.usd >= MAX_USD:
            return stop("Investigation planning budget reached; existing evidence is preserved.")
        try:
            decision = review(self.llm(state), self.catalog, state.get("objective") or state["question"],
                              results, ledger)
            if decision.action == "stop" or not decision.probes:
                return stop("No further supported probe proposed. Observed contributions do not prove causality.")
            items, notes = [], []
            for probe in decision.probes:
                spec, note = resolve_probe(probe, results, self.catalog)
                spec = retain_scope(spec, results[0].get("spec"), self.catalog)
                items.append({"spec": spec.model_dump(mode="json"), "purpose": "Test hypothesis: " + probe.hypothesis})
                notes.append(note)
            prepared = self.prepare({"prepared": items})
            if len(results) + len(prepared) > MAX_QUERIES:
                return stop("Investigation query budget reached.")
            seen = {r["sql"] for r in results}
            if any(p["sql"] in seen for p in prepared) or len({p["sql"] for p in prepared}) != len(prepared):
                return stop("The proposed probe repeats an executed query; investigation stopped.")
        except Exception:
            log.exception("adaptive investigation planning failed")
            return stop("Further investigation planning failed; completed evidence is preserved.")
        version = state.get("plan_version", 0) + 1
        summary = "\n\n".join("**Unproven hypothesis:** " + note["hypothesis"] +
            "\n**Alternative explanation:** " + note["competing_explanation"] + "\n" +
            readback(QuerySpec.model_validate(item["spec"]), self.catalog)
            for item, note in zip(prepared, notes, strict=True))
        return {**base, "probe_ledger": [*ledger, *notes], "prepared": prepared,
                "spec": prepared[0]["spec"], "template": None, "readback": summary,
                "restated": "test the next hypotheses using the completed evidence",
                "plan_version": version, "plan_digest": plan_digest(prepared, version, self.snapshot),
                "confirmed_digest": None, "fast_path": False, "revision": "", "answers": {}, "choices": {},
                "open_points": [OpenPoint("confirm", CONFIRM_QUESTION, [], "", "Approve the next probe").__dict__],
                "route": "clarify", "_status": "needs_input"}

    def _analyse_one(self, state: State) -> dict:
        frame = _frame(state["result"])
        notes = "\n".join(f"- {n}: {self.metrics.metrics[n].notes}" for n in state.get("metric_names") or []
                          if self.metrics.metrics[n].notes)
        facts = list(state.get("notes") or [])
        if state["result"].get("truncated"):
            facts.append("The result is truncated; do not treat partial rows as the complete population.")
        units = {c: RESULT_UNITS[c] for c in frame.columns if c in RESULT_UNITS} if state.get("template") else {}
        if state.get("spec"):
            spec = QuerySpec.model_validate(state["spec"])
            measures = {m.name: m for m in spec.measures}
            for m in spec.measures:
                if m.metric:
                    units[m.name] = self.metrics.metrics[m.metric].unit
                elif m.ratio or m.agg == "share":
                    money_ratio = m.ratio and all(measures[n].metric and
                        self.metrics.metrics[measures[n].metric].unit == "inr" for n in m.ratio)
                    units[m.name] = "multiple" if money_ratio else "ratio"
                elif m.agg in ("count", "count_distinct"):
                    units[m.name] = "count"
            if not spec.operation:
                units.update({f"{name}_share": "ratio" for name in spec.shares})
            operation = spec.operation
            if operation:
                for name, unit in list(units.items()):
                    if operation.kind == "period_compare":
                        units.update({f"{name}_contribution": "ratio", f"{name}_previous": unit, f"{name}_delta":
                                      "pct_points" if unit == "ratio" else unit, f"{name}_change_ratio": "ratio"})
                    else:
                        units[f"{name}_{operation.kind}"] = unit
        units = {name: unit for name, unit in units.items() if name in frame.columns}
        notes += "\nResult units: " + ", ".join(f"{c}={u}" for c, u in units.items())
        args = (self.llm(state), state["question"], state.get("readback", ""), state["sql"], frame,
                state.get("memory", ""), notes, self.data)
        analysis = (deterministic_analysis(frame, units) if state.get("fast_path") or (len(frame) <= 1 and units)
                    else agents.analyse(*args, concerns=facts))
        def checked(a):
            check = verify(frame, a.claims, a.texts(), units=units, truncated=state["result"].get("truncated", False))
            mentioned = {c.column for c in check.claims if any(
                "{" + c.id + "}" in text for text in [a.headline, *a.findings])}
            missing = [c for c in units if c in frame and frame[c].notna().any() and c not in mentioned]
            if missing:
                check.problems.append("summary omits requested measures: " + ", ".join(missing))
                check.ok = False
            return check
        check = checked(analysis)
        rewrote = not check.ok
        if rewrote:                                                         # one rewrite with the exact problems
            analysis = agents.analyse(*args, fix_numbers=check.problems, concerns=facts)
            check = checked(analysis)
        if not check.ok:                     # an uncheckable sentence is removed; the verified table still answers
            analysis = drop_unverified(analysis, check.problems)
            check = checked(analysis)
        if not check.ok and units:
            analysis = deterministic_analysis(frame, units)
            check = checked(analysis)
        # Render database-derived values, even when the model's rounded approximation was acceptable.
        analysis = analysis.model_copy(update={"claims": check.claims})
        shown = analysis.rendered()
        warnings = [*state.get("warnings", []), *facts]
        return {"analysis": shown.model_dump(), "grounded": check.ok, "warnings": warnings, "route": "chart",
                "_detail": {"headline": shown.headline, "number_check": "passed" if check.ok else "removed",
                            "claims": check.checked, "rewritten": rewrote}}

    @step("chart")
    def chart(self, state: State) -> dict:
        items = state.get("analyses") or [{"analysis": state["analysis"], "result": state["result"]}]
        for item in items:
            a = item["analysis"]
            item["chart"] = build_chart(_frame(item["result"]), a.get("chart_title") or a["headline"],
                                        a.get("chart_subtitle", ""), a.get("chart_metric"))
        return {"analyses": items, "chart": items[0]["chart"], "_detail": {"charts": len(items)}}

    @step("remember")
    def remember(self, state: State) -> dict:
        a = state["analysis"]
        if self.snapshot and snapshot_signature() != self.snapshot:
            return {"insight_id": None, "status": "failed",
                    "message": "The data changed during analysis. Please ask again; no insight was saved."}
        if state.get("grounded") is not True or any(
                item["result"].get("truncated") for item in state.get("results", [])):
            return {"insight_id": None, "status": "done", "_status": "skipped",
                    "_detail": {"saved": False, "reason": "not saved: the summary or result is incomplete"}}
        memory = self.mem(state)
        plan = {"reading": state.get("readback"), "spec": state.get("spec"), "template": state.get("template"),
                "metrics": state.get("metric_names") or []}
        item = memory.save_insight(question=state["question"], plan=plan, sql=state["sql"], analysis=a,
                                   data_as_of=self.data["last_day"], chat_id=state.get("chat_id"))
        closed = memory.close_gaps(a.get("explains_gap_ids", []), item["id"], self.data["last_day"])
        return {"insight_id": item["id"], "status": "done",
                "_detail": {"facts": len(a.get("memory", [])), "gaps_closed": closed}}

    def finish(self, state: State) -> dict:
        return {}

    # ------------------------------------------------------------------ graph
    def _build(self) -> StateGraph:
        g = StateGraph(State)
        for name in ["check_input", "interpret", "clarify", "execute",
                     "analyse", "reflect", "chart", "remember", "finish"]:
            g.add_node(name, getattr(self, name))
        route = lambda s: s["route"]                                       # noqa: E731
        g.add_edge(START, "check_input")
        g.add_conditional_edges("check_input", route, ["interpret", "finish"])
        g.add_conditional_edges("interpret", route, ["clarify", "execute", "interpret", "finish"])
        g.add_conditional_edges("clarify", route, ["interpret", "execute", "clarify", "finish"])
        g.add_conditional_edges("execute", route, ["analyse", "finish"])
        g.add_edge("analyse", "reflect")
        g.add_conditional_edges("reflect", route, ["chart", "clarify"])
        g.add_edge("chart", "remember")
        g.add_edge("remember", "finish")
        g.add_edge("finish", END)
        return g

    # ------------------------------------------------------------------ running
    def start(self, question: str, chat_id: str | None = None, history: str = "", use_defaults: bool = False,
              memory: Any = None, run_id: str | None = None,
              previous_spec: dict | None = None, investigation: dict | None = None) -> tuple[str, Iterator[dict]]:
        """memory: a scope with context(), save_insight() and close_gaps(); defaults to the file memory."""
        run_id = run_id or uuid.uuid4().hex[:12]
        if memory is not None:
            self._memories[run_id] = memory
        state = {"run_id": run_id, "chat_id": chat_id, "question": question, "history": history,
                 "previous_spec": previous_spec, "plan_version": 0, "investigation": investigation or {}}
        return run_id, self._stream(run_id, state)

    def resume(self, run_id: str, message: str | dict | None, plan_version: int | None = None) -> Iterator[dict]:
        pending = self.graph.get_state({"configurable": {"thread_id": run_id}})
        if not pending.next:
            raise ValueError("This run has no pending proposal; it cannot execute again")
        # Older callers may supply an explicit answer map; empty maps never imply approval.
        if isinstance(message, dict):
            message = " ".join(str(v) for v in message.values())
        return self._stream(run_id, Command(resume={"message": message or "", "plan_version": plan_version}))

    def _stream(self, run_id: str, payload: Any) -> Iterator[dict]:
        config = {"configurable": {"thread_id": run_id}, "recursion_limit": 100}
        try:
            for mode, chunk in self.graph.stream(payload, config, stream_mode=["custom", "updates"]):
                if mode == "custom":
                    yield chunk
                elif "__interrupt__" in chunk:
                    llm = self._llms.get(run_id)            # spend so far (the run stays open for the answer)
                    yield {"type": "clarify", "run_id": run_id, **chunk["__interrupt__"][0].value,
                           "cost": llm.tracker.summary() if llm else None}
                    return
        except BudgetExceeded as error:
            yield {**self.final(run_id), "status": "failed", "message": str(error)}
            return
        except Exception:
            self._llms.pop(run_id, None)
            self._memories.pop(run_id, None)
            raise
        yield self.final(run_id)

    def final(self, run_id: str) -> dict:
        state = self.graph.get_state({"configurable": {"thread_id": run_id}}).values
        llm = self._llms.pop(run_id, None)
        self._memories.pop(run_id, None)
        primary = (state.get("completed_results") or [{}])[0]
        result = {"type": "final", "run_id": run_id, "status": state.get("status"), "message": state.get("message"),
                "question": state.get("question"), "restated": state.get("restated"),
                "readback": (readback(QuerySpec.model_validate(primary["spec"]), self.catalog)
                             if primary.get("spec") else state.get("readback")),
                "spec": primary.get("spec", state.get("spec")),
                "confirmed_plan": ([{k: v for k, v in r.items() if k not in ("result", "metric_names")}
                                    for r in state["completed_results"]] if state.get("completed_results") else
                                    state.get("prepared") if state.get("confirmed_digest") else None),
                "objective": state.get("objective") or state.get("question"),
                "relationship": state.get("relationship", "new"),
                "metric_definitions": {n: self.metrics.metrics[n].model_dump(mode="json")
                                       for n in {name for item in
                                                 (state.get("completed_results") or state.get("results", []))
                                                 for name in item.get("metric_names", [])}},
                "analyses": (state.get("analyses", []) if state.get("status") == "done" else
                             state.get("completed_analyses") or state.get("analyses", [])),
                "context_sources": [{k: e.get(k) for k in ("run_id", "question", "created_at", "stale")}
                                    for e in state.get("investigation", {}).get("retrieved_evidence", [])],
                "investigation_stop": state.get("investigation_stop"),
                "probe_ledger": state.get("probe_ledger", []),
                "template": primary.get("template", state.get("template")),
                "populations": populations(state.get("completed_results") or state.get("results", []),
                                           state.get("completed_results") or state.get("results", []),
                                           self.catalog, self.snapshot) if state.get("grounded") else [],
                "trust": state.get("trust"), "sql": primary.get("sql", state.get("sql")),
                "result": primary.get("result", state.get("result")),
                "analysis": state.get("analysis"), "chart": state.get("chart"),
                "warnings": state.get("warnings", []), "insight_id": state.get("insight_id"),
                "grounded": state.get("grounded"), "verification": state.get("verification"),
                "data_as_of": self.data["last_day"], "data_size": self.data["size"],
                "cost": llm.tracker.summary() if llm else None,
                "calls": [c.__dict__ for c in llm.tracker.calls] if llm else []}
        if isinstance(self._saver, InMemorySaver):
            self._saver.delete_thread(run_id)  # terminal results live in the app store, not unbounded RAM
        return result


def spec_key(spec: dict | None) -> str | None:
    """What a spec computes, for comparing with an intended spec (evaluation)."""
    return canonical(QuerySpec.model_validate(spec)) if spec else None


def deterministic_analysis(frame: pd.DataFrame, units: dict[str, str]) -> agents.Analysis:
    """Describe a few cells without making an untested causal, trend or ranking assertion."""
    claims, findings = [], []
    keys = [c for c in frame.columns if c not in units]
    for _, row in frame.head(5).iterrows():
        for column, unit in units.items():
            if column not in row or pd.isna(row[column]):
                continue
            claim = Claim(id=f"c{len(claims) + 1}", value=float(row[column]), unit=unit, column=column,
                          row=[KeyValue(column=k, value=str(row[k])) for k in keys])
            claims.append(claim)
            label = ", ".join(str(row[k]) for k in keys)
            # Dimension labels may contain numbers; keep labels in the result table in that case.
            label = "" if free_numbers(label) else label
            findings.append(f"{label + ': ' if label else ''}{column.replace('_', ' ')} = {{{claim.id}}}")
    return agents.Analysis(headline=findings[0] if findings else "No observed value for the requested scope.",
                           findings=findings[1:], claims=claims, chart_metric=next(iter(units), None),
                           chart_title="Verified result")


# ----------------------------------------------------------------------------- command line
def main() -> None:
    """Ask and confirm an analytical plan from the terminal."""
    import argparse

    parser = argparse.ArgumentParser(prog="pantree-ask")
    parser.add_argument("question", nargs="+")
    args = parser.parse_args()
    question = " ".join(args.question)
    agent = Agent()
    print(f"\n{question}\n")
    run_id, events = agent.start(question)
    while True:
        final = None
        for event in events:
            if event.get("type") == "step" and event["status"] != "started":
                print(f"  · {event['step']} {event['status']} {event.get('detail', {}).get('seconds', '')}s")
            if event["type"] in ("clarify", "final"):
                final = event
        if final and final["type"] == "clarify":
            print("\n" + final["message"])
            events = agent.resume(run_id, input("You: ").strip(), final["plan_version"])
            continue
        break
    if final["status"] != "done":
        print(f"\n{final['status'].upper()}: {final.get('message')}")
        return
    a = final["analysis"]
    print(f"\n{a['headline']}\n" + "".join(f"  • {f}\n" for f in a["findings"]))
    print(f"SQL (verified):\n{final['sql']}")
