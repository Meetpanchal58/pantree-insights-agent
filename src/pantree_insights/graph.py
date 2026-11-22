"""The agent as a LangGraph state machine. The model decides which question is answered, never which number.

    check_input -> interpret -> clarify (questions or read-back; pauses for the user) -> interpret ...
                   interpret -> execute (compiled spec or reviewed template; no model writes SQL)
                   execute -> analyse (numbers rendered by code) -> chart -> remember -> finish

Every step streams progress events ({"type": "step", "step", "status", "detail", ...}). A run pauses at a question
or the read-back and resumes with Agent.resume(). See docs/ARCHITECTURE.md.
"""
from __future__ import annotations

import contextlib
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
from .db import connect, manifest, run_query, snapshot_signature
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
    use_defaults: bool
    answers: dict[str, str]
    choices: dict[str, str]
    open_points: list[dict]
    clarify_rounds: int
    confirmed: bool
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
                  seconds=round(time.perf_counter() - started, 2), **detail)
            if isinstance(update, dict):
                update.pop("_status", None)
            return update
        return node
    return wrap


class Agent:
    def __init__(self, llm_factory: Callable[[], LLM] | None = None, memory: Memory | None = None, con=None,
                 data: dict | None = None):
        settings = get_settings()
        self.llm_factory = llm_factory or ChatLLM
        started = time.monotonic()
        log.info("agent_prepare phase=duckdb started")
        self.con = con or connect(settings.shop_dir)
        log.info("agent_prepare phase=duckdb seconds=%.2f", time.monotonic() - started)
        self.memory = memory or Memory()
        self.metrics, self.glossary = load_metrics(), load_glossary()
        self.snapshot = snapshot_signature() if data is None else None
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

            dsn = settings.database_url.replace("postgresql+psycopg://", "postgresql://")
            if not dsn.startswith(("postgresql://", "postgres://")):
                raise RuntimeError("CHECKPOINT_BACKEND=postgres requires a PostgreSQL DATABASE_URL")
            self._checkpoint_context = PostgresSaver.from_conn_string(dsn)
            saver = self._checkpoint_context.__enter__()
            saver.setup()
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
        if background is None:
            background = self.mem(state).context(state["question"], as_of=self.data["last_day"])
        pick = choices.pop("reading", None)
        if pick is not None and state.get("readings"):           # the user chose one of the readings
            spec = QuerySpec.model_validate(state["readings"][int(pick)])
            decision = Decision("spec", spec=spec, restated=state.get("restated", state["question"]))
        else:
            decision = decide(self.llm(state), self.catalog, self.glossary.terms, self.aliases, self.data,
                              state["question"], state.get("history", ""), answers, self.glossary.ambiguous,
                              contrast=get_settings().contrast_check, background=background)
        detail = {"decision": decision.kind, "restated": decision.restated, "reason": decision.reason,
                  "fast_path": bool(decision.detail.get("fast_path"))}
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
            if state.get("use_defaults") and pending and not any(p.required for p in pending):
                answers.update({p.question: p.default for p in pending})
                return {"answers": answers, "clarify_rounds": rounds + 1, "route": "interpret",
                        "_detail": {**detail, "defaults": [p.question for p in pending]}}
            return {"open_points": [p.__dict__ for p in pending], "route": "clarify", "_status": "needs_input",
                    "memory": background,
                    "readings": [r.model_dump(mode="json") for r in decision.readings],
                    "restated": decision.restated,
                    "_detail": {**detail, "round": rounds + 1, "questions": [p.question for p in pending]}}
        if decision.kind == "template":
            _, summary = build_template(decision.template.name, decision.template.params)
            summary = f"**Analysis:** {summary}" + (f"\n**Assumed:** {'; '.join(decision.assumptions)}"
                                                     if decision.assumptions else "")
            out = {"template": {"name": decision.template.name, "params": decision.template.params}, "spec": None}
        else:
            summary = readback(decision.spec, self.catalog, decision.assumptions)
            out = {"spec": decision.spec.model_dump(mode="json"), "template": None}
        out.update({"readback": summary, "restated": decision.restated, "notes": decision.assumptions,
                    "trust": None, "open_points": [], "answers": answers, "choices": choices,
                    "memory": background, "fast_path": bool(decision.detail.get("fast_path")),
                    "_detail": {**detail, "readback": summary}})
        if not out["fast_path"] and not state.get("use_defaults") and not state.get("confirmed"):
            point = OpenPoint("confirm", CONFIRM_QUESTION, [CONFIRM_YES, CONFIRM_NO], CONFIRM_YES,
                              "nothing is computed until you confirm", {CONFIRM_YES: "yes"})
            return {**out, "open_points": [point.__dict__], "route": "clarify", "_status": "needs_input"}
        return {**out, "route": "execute"}

    def clarify(self, state: State) -> dict:
        """Pause for the user's answers (or confirmation), then go back to interpret or on to execute."""
        points = [OpenPoint(**p) for p in state.get("open_points") or []]
        questions = [p.public() for p in points]
        rounds = state.get("clarify_rounds", 0) + 1
        confirming = any(p.key == "confirm" for p in points)
        payload = {"type": "clarify", "kind": "confirm" if confirming else "clarify", "questions": questions,
                   "round": rounds}
        if confirming:
            payload.update(readback=state.get("readback", ""), spec=state.get("spec"), template=state.get("template"))
        given = interrupt(payload)                                          # pauses the run here
        if not isinstance(given, dict) or not given:                        # "just use defaults"
            given = {q["question"]: q["default"] for q in questions}
        if confirming:
            answer = (given.get(CONFIRM_QUESTION) or CONFIRM_YES).strip()
            if answer == CONFIRM_YES:
                return {"confirmed": True, "clarify_rounds": rounds, "open_points": [], "route": "execute"}
            correction = "The user wants something changed but did not say what: ask what to change" \
                if answer == CONFIRM_NO else answer
            return {"answers": {**(state.get("answers") or {}), CORRECTION: correction}, "confirmed": False,
                    "clarify_rounds": rounds, "open_points": [], "spec": None, "template": None, "route": "interpret"}
        answers, choices = {**(state.get("answers") or {}), **given}, dict(state.get("choices") or {})
        for p in points:
            answer = given.get(p.question, p.default)
            if p.meaning and answer in p.meaning:
                choices[p.key] = p.meaning[answer]
        return {"answers": answers, "choices": choices, "clarify_rounds": rounds, "open_points": [],
                "confirmed": False, "route": "interpret"}

    @step("execute")
    def execute(self, state: State) -> dict:
        """Compile the confirmed spec (or build the template) and run it. No model writes this SQL."""
        try:
            if state.get("template"):
                call = TemplateCall.model_validate(state["template"])
                sql, _ = build_template(call.name, call.params)
                metrics: list[str] = []
            else:
                spec = QuerySpec.model_validate(state["spec"])
                problems = validate(spec, self.catalog)
                if problems:
                    raise SpecError("; ".join(problems))
                sql = compile_spec(spec, self.catalog)
                metrics = [m.metric for m in spec.measures if m.metric]
            if not check_sql(sql).ok:                                       # defence in depth: same safety rules
                raise SpecError("the verified query failed the safety checks")
            started = time.perf_counter()
            frame, truncated = self.query(sql)
            if self.snapshot and snapshot_signature() != self.snapshot:
                raise SpecError("the data changed during execution; please ask again")
        except (SpecError, TemplateError) as error:
            return {"status": "failed", "route": "finish", "message": f"The verified query could not be built: {error}"}
        result = _result(frame, truncated)
        verification = {"status": "verified", "how": "compiled from governed definitions; SQL safety checked",
                        "scope": "query and numeric evidence; not a proof of natural-language interpretation"}
        with contextlib.suppress(RuntimeError):
            get_stream_writer()({"type": "result", "run_id": state["run_id"], "result": result,
                                 "sql": sql, "readback": state.get("readback"), "trust": "verified",
                                 "verification": verification, "data_as_of": self.data["last_day"],
                                 "data_size": self.data["size"], "warnings": state.get("notes", [])})
        return {"sql": sql, "metric_names": metrics, "result": result, "trust": "verified",
                "verification": verification,
                "route": "analyse",
                "_detail": {"sql": sql, "rows": len(frame), "columns": list(frame.columns), "truncated": truncated,
                            "query_seconds": round(time.perf_counter() - started, 2)}}

    @step("analyse")
    def analyse(self, state: State) -> dict:
        frame = _frame(state["result"])
        notes = "\n".join(f"- {n}: {self.metrics.metrics[n].notes}" for n in state.get("metric_names") or []
                          if self.metrics.metrics[n].notes)
        facts = list(state.get("notes") or [])
        if state["result"].get("truncated"):
            facts.append("The result is truncated; do not treat partial rows as the complete population.")
        units = {c: RESULT_UNITS[c] for c in frame.columns if c in RESULT_UNITS} if state.get("template") else {}
        if state.get("spec"):
            for m in QuerySpec.model_validate(state["spec"]).measures:
                if m.metric:
                    units[m.name] = self.metrics.metrics[m.metric].unit
                elif m.ratio or m.agg == "share":
                    units[m.name] = "ratio"
                elif m.agg in ("count", "count_distinct"):
                    units[m.name] = "count"
        notes += "\nResult units: " + ", ".join(f"{c}={u}" for c, u in units.items())
        args = (self.llm(state), state["question"], state.get("readback", ""), state["sql"], frame,
                state.get("memory", ""), notes, self.data)
        analysis = (deterministic_analysis(frame, units) if state.get("fast_path")
                    else agents.analyse(*args, concerns=facts))
        def checked(a):
            return verify(frame, a.claims, a.texts(), units=units, truncated=state["result"].get("truncated", False))
        check = checked(analysis)
        rewrote = not check.ok
        if rewrote:                                                         # one rewrite with the exact problems
            analysis = agents.analyse(*args, fix_numbers=check.problems, concerns=facts)
            check = checked(analysis)
        if not check.ok:                     # an uncheckable sentence is removed; the verified table still answers
            analysis = drop_unverified(analysis, check.problems)
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
        a = state["analysis"]
        chart = build_chart(_frame(state["result"]), a.get("chart_title") or a["headline"],
                            a.get("chart_subtitle", ""), a.get("chart_metric"))
        return {"chart": chart, "_detail": {"kind": (chart or {}).get("kind")}}

    @step("remember")
    def remember(self, state: State) -> dict:
        a = state["analysis"]
        if self.snapshot and snapshot_signature() != self.snapshot:
            return {"insight_id": None, "status": "failed",
                    "message": "The data changed during analysis. Please ask again; no insight was saved."}
        if state.get("grounded") is not True or (state.get("result") or {}).get("truncated"):
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
        for name in ["check_input", "interpret", "clarify", "execute", "analyse", "chart", "remember", "finish"]:
            g.add_node(name, getattr(self, name))
        route = lambda s: s["route"]                                       # noqa: E731
        g.add_edge(START, "check_input")
        g.add_conditional_edges("check_input", route, ["interpret", "finish"])
        g.add_conditional_edges("interpret", route, ["clarify", "execute", "interpret", "finish"])
        g.add_conditional_edges("clarify", route, ["interpret", "execute"])
        g.add_conditional_edges("execute", route, ["analyse", "finish"])
        g.add_edge("analyse", "chart")
        g.add_edge("chart", "remember")
        g.add_edge("remember", "finish")
        g.add_edge("finish", END)
        return g

    # ------------------------------------------------------------------ running
    def start(self, question: str, chat_id: str | None = None, history: str = "", use_defaults: bool = False,
              memory: Any = None, run_id: str | None = None) -> tuple[str, Iterator[dict]]:
        """memory: a scope with context(), save_insight() and close_gaps(); defaults to the file memory."""
        run_id = run_id or uuid.uuid4().hex[:12]
        if memory is not None:
            self._memories[run_id] = memory
        state = {"run_id": run_id, "chat_id": chat_id, "question": question, "history": history,
                 "use_defaults": use_defaults}
        return run_id, self._stream(run_id, state)

    def resume(self, run_id: str, answers: dict[str, str] | None) -> Iterator[dict]:
        return self._stream(run_id, Command(resume=answers or {}))

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
        result = {"type": "final", "run_id": run_id, "status": state.get("status"), "message": state.get("message"),
                "question": state.get("question"), "restated": state.get("restated"),
                "readback": state.get("readback"), "spec": state.get("spec"),
                "template": state.get("template"), "trust": state.get("trust"), "sql": state.get("sql"),
                "result": state.get("result"), "analysis": state.get("analysis"), "chart": state.get("chart"),
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
    """pantree-ask [--defaults] "question": ask the agent from the terminal."""
    import argparse

    parser = argparse.ArgumentParser(prog="pantree-ask")
    parser.add_argument("question", nargs="+")
    parser.add_argument("--defaults", action="store_true", help="answer questions with their defaults")
    args = parser.parse_args()
    question = " ".join(args.question)
    agent = Agent()
    print(f"\n{question}\n")
    run_id, events = agent.start(question, use_defaults=args.defaults)
    while True:
        final = None
        for event in events:
            if event.get("type") == "step" and event["status"] != "started":
                print(f"  · {event['step']} {event['status']} {event.get('detail', {}).get('seconds', '')}s")
            if event["type"] in ("clarify", "final"):
                final = event
        if final and final["type"] == "clarify":
            if final.get("readback"):
                print("\nHere's what I'll compute:\n  " + final["readback"].replace("**", "").replace("\n", "\n  "))
            answers = {}
            for q in final["questions"]:
                answer = input(f"  {q['question']} [{' / '.join(q['options'])}] (default: {q['default']}): ").strip()
                answers[q["question"]] = answer or q["default"]
            events = agent.resume(run_id, answers)
            continue
        break
    if final["status"] != "done":
        print(f"\n{final['status'].upper()}: {final.get('message')}")
        return
    a = final["analysis"]
    print(f"\n{a['headline']}\n" + "".join(f"  • {f}\n" for f in a["findings"]))
    print(f"SQL (verified):\n{final['sql']}")
