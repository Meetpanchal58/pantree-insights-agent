"""The agent flow, as a LangGraph state machine.

    check_input -> retrieve -> understand --(open points: pause for the user's answers)--> understand ...
                               understand -> plan --(decline)-> finish
                               plan -> write_sql -> check_sql -> run_query -> validate -> analyse
                                          ^________|___________|____________|   (feedback, max 3 tries)
                               analyse -> (number check, one rewrite) -> chart -> remember -> finish

Every step streams progress events ({"type": "step", "step", "status", "detail", ...}) so the UI can
show the agents working. A run pauses at clarifying questions and resumes with Agent.resume().
"""
from __future__ import annotations

import contextlib
import re
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date
from functools import wraps
from typing import Any, TypedDict

import pandas as pd
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from . import agents
from .catalog import value_columns as catalog_values
from .charts import build_chart
from .checks import result_problems
from .config import get_settings
from .db import QueryTimeout, connect, manifest, run_query, snapshot_signature
from .grounding import verify
from .guardrails import check_question, check_sql
from .joins import JoinGraph
from .llm import LLM, BudgetExceeded, ChatLLM
from .memory import Memory
from .profile import profile_for
from .recompute import canonical_sql, check_answer
from .references import ReferenceIndex
from .retrieval import Retriever, get_retriever
from .semantic import load_concepts, load_metrics, tables_in
from .understanding import IdResolver, OpenPoint, readback, resolve

MAX_CLARIFY_ROUNDS = 6        # stop without computing if material questions remain unresolved
CONFIRM_QUESTION = "Is this what you want me to compute?"
CONFIRM_YES, CONFIRM_NO = "Yes, go ahead", "No, change something"
CORRECTION = "Correction to your reading of the question"


@dataclass(frozen=True)
class Features:
    """Switches for ablations (evals/run_eval.py --variant). The default is the full system."""
    retrieval: bool = True        # hybrid retrieval context (off: the whole schema.yaml)
    semantic: bool = True         # metric definitions and glossary in the context
    planner: bool = True          # planner step (off: the question goes straight to the SQL writer)
    validator: bool = True        # validator retry loop
    narrow: bool = True           # SQL writer's first attempt sees only the plan's tables
    memory: bool = True           # memory in prompts and saving insights
    understand: bool = True       # question understanding + clarifying loop + code-resolved columns and joins


VARIANTS = {
    "baseline": Features(retrieval=False, semantic=False, planner=False, validator=False, narrow=False, memory=False,
                         understand=False),
    "semantic": Features(retrieval=False, semantic=True, planner=False, validator=False, narrow=False, memory=False,
                         understand=False),
    "retrieval": Features(retrieval=True, semantic=True, planner=False, validator=False, narrow=False, memory=False,
                          understand=False),
    "planner": Features(retrieval=True, semantic=True, planner=True, validator=False, narrow=False, memory=False,
                        understand=False),
    "validator": Features(retrieval=True, semantic=True, planner=True, validator=True, narrow=False, memory=False,
                          understand=False),
    "no_understanding": Features(understand=False),      # the system before the understanding step
    "full": Features(),
    "full_no_memory": Features(memory=False),
}


class State(TypedDict, total=False):
    run_id: str
    chat_id: str | None
    question: str
    history: str
    use_defaults: bool
    exclude_examples: list[str]  # evaluation: hide these example ids from retrieval (no leakage)
    answers: dict[str, str]
    frame: dict                  # the understanding agent's question frame
    understanding: str           # resolved subjects, filters, columns and join paths (prompt text)
    understood_tables: list[str] # tables the resolved understanding needs (kept in the narrowed context)
    resolved_filters: list[list[str]]  # [column, value] the SQL must use (checked after the query runs)
    references: str              # reference queries with the same structure (tables, columns, metrics)
    open_points: list[dict]      # questions waiting for the user (with their internal meaning)
    choices: dict[str, str]      # answers code can act on: "mention:Mumbai" -> "city"
    clarify_rounds: int
    confirmed: bool              # the user confirmed the read-back of the understood question
    verify_scope: dict           # confirmed scope for recomputing the answer's metrics (recompute.py)
    verification: dict           # {"status": verified | mismatch | unverified, "metrics": {name: status}}
    readback: str
    context: str
    retrieved: dict              # the retrieved context's parts, for narrowing it after planning
    metric_names: list[str]
    memory: str
    plan: dict
    sql: str
    explanation: str
    attempts: int
    feedback: list[str]
    result: dict                 # {"columns": [...], "data": [[...]], "truncated": bool}
    warnings: list[str]
    analysis: dict
    grounded: bool
    chart: dict | None
    insight_id: str | None
    status: str                  # running | clarify | declined | failed | done
    message: str                 # for declined / failed
    route: str


def _frame(result: dict) -> pd.DataFrame:
    return pd.DataFrame(result["data"], columns=result["columns"])


def _result(frame: pd.DataFrame, truncated: bool) -> dict:
    import json

    split = json.loads(frame.to_json(orient="split", date_format="iso", index=False))
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
                  seconds=round(time.perf_counter() - started, 2), **detail)
            if isinstance(update, dict):
                update.pop("_status", None)
            return update
        return node
    return wrap


class Agent:
    def __init__(self, llm_factory: Callable[[], LLM] | None = None, retriever: Retriever | None = None,
                 memory: Memory | None = None, con=None, data: dict | None = None,
                 features: Features | None = None):
        settings = get_settings()
        self.features = features or Features()
        self.llm_factory = llm_factory or ChatLLM
        self.con = con or connect(settings.shop_dir)
        self.retriever = retriever or get_retriever()
        self.memory = memory or Memory()
        self.metrics = load_metrics()
        self.snapshot = snapshot_signature() if data is None else None
        info = data or manifest(settings.shop_dir)
        self.data = {"first_day": info["first_day"], "last_day": info["last_day"], "size": info["size"]}
        self.concepts = load_concepts()
        if self.features.understand:
            profile = profile_for(self.con, {**info, "snapshot_signature": self.snapshot})
            self.joins = JoinGraph(self.retriever.schema, profile)
            self.ids = IdResolver(profile.id_formats(), self.concepts.id_entities, self.con)
            self.aliases = {alias.lower(): value for value, names in self.retriever.glossary.value_aliases.items()
                            for alias in names}
            self.references = ReferenceIndex(list(self.retriever.examples.values()))
            # which columns hold a value: the catalog's complete lists plus the runtime value index (brands, ...)
            self.value_columns = {v: list(cols) for v, cols in self.retriever.value_columns.items()}
            for value, cols in catalog_values().items():
                self.value_columns[value] = sorted(set(self.value_columns.get(value, [])) | set(cols))
        self._llms: dict[str, LLM] = {}
        self._memories: dict[str, Any] = {}          # run_id -> memory scope for that question
        self._checkpoint_context = None
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
        self.graph = self._build().compile(checkpointer=saver)

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

    # ------------------------------------------------------------------ nodes
    @step("check_input")
    def check_input(self, state: State) -> dict:
        verdict = check_question(state["question"])
        if not verdict.ok:
            return {"status": "declined", "message": verdict.reason, "route": "finish", "_status": "blocked",
                    "_detail": {"reason": verdict.reason}}
        return {"status": "running", "route": "retrieve", "attempts": 0, "warnings": [], "feedback": []}

    @step("retrieve")
    def retrieve(self, state: State) -> dict:
        f = self.features
        query = state["question"]
        if state.get("history"):
            query += "\nEarlier chat: " + state["history"]
        context = self.retriever.retrieve(query, set(state.get("exclude_examples") or []))
        if not f.retrieval:
            prompt = "## Tables\n" + self.retriever.schema.describe() + "\n\n## How tables join\n" + "\n".join(
                f"- {j}" for j in self.retriever.schema.joins)
            if f.semantic:
                prompt += "\n\n## Metric definitions (use these exactly)\n" + self.metrics.describe()
                prompt += "\n\n## Business terms\n" + "\n".join(
                    f"- {t}: {m}" for t, m in self.retriever.glossary.terms.items())
            return {"context": prompt, "retrieved": {}, "metric_names": [],
                    "memory": self.mem(state).context(state["question"], as_of=self.data["last_day"])
                    if f.memory else "(memory disabled)",
                    "_detail": {"tables": list(self.retriever.schema.tables), "mode": "full schema"}}
        retrieved = {"tables": context.tables, "metrics": context.metrics, "joins": context.joins,
                     "values": [[v.value, v.columns, v.score] for v in context.values],
                     "examples": [e.id for e in context.examples], "terms": context.terms}
        return {"context": context.to_prompt(self.retriever.schema, self.metrics), "retrieved": retrieved,
                "metric_names": context.metrics,
                "memory": self.mem(state).context(state["question"], as_of=self.data["last_day"])
                if f.memory else "(memory disabled)",
                "_detail": {"tables": context.tables, "metrics": context.metrics,
                            "values": [v.value for v in context.values[:6]],
                            "examples": [e.question for e in context.examples[:3]]}}

    @step("understand")
    def understand(self, state: State) -> dict:
        """What exactly is asked? The LLM reads the question (with every answer so far); code resolves the words
        to columns and joins and adds what the LLM missed. Anything unclear goes to the user, round after round,
        until nothing is left (or the safety stop is reached, or the user asked for defaults)."""
        if not self.features.understand:
            return {"route": "plan", "_status": "skipped", "_detail": {"reason": "understanding disabled"}}
        answers, choices = dict(state.get("answers") or {}), dict(state.get("choices") or {})
        rounds = state.get("clarify_rounds", 0)
        frame = agents.understand(self.llm(state), state["question"], state["context"], self.data, self.concepts,
                                  state.get("history", ""), answers)
        if frame.decline_reason:                           # decided first, before the question is resolved
            return {"status": "declined", "message": frame.decline_reason, "route": "finish", "_status": "declined",
                    "_detail": {"decision": "decline", "reason": frame.decline_reason}}
        ids = self.ids.find(state["question"])
        res = resolve(frame, self.concepts, self.joins, self.value_columns, ids, choices, self.aliases)
        asked = {q.strip().lower() for q in answers}
        pending = [OpenPoint(f"llm:{q.question}", q.question, q.options, q.default, q.why)
                   for q in frame.open_points if q.question.strip().lower() not in asked] + \
                  [p for p in res.open_points if p.key not in choices]
        warnings = list(state.get("warnings", []))
        if pending and rounds >= MAX_CLARIFY_ROUNDS:
            return {"status": "declined", "route": "finish", "open_points": [],
                    "message": "The question is still unclear. Please restate the metric, period and filters; "
                               "nothing was computed from guessed values."}
        if pending and (not state.get("use_defaults") or any(p.required for p in pending)):
            return {"frame": frame.model_dump(), "open_points": [p.__dict__ for p in pending], "route": "clarify",
                    "_status": "needs_input",
                    "_detail": {"round": rounds + 1, "questions": [p.question for p in pending],
                                "understood": frame.restated_question}}
        if pending:                                        # defaults: the user asked for them, or the safety stop
            for p in pending:
                answers[p.question] = p.default
                if p.meaning:
                    choices[p.key] = p.meaning.get(p.default, p.default)
            return {"answers": answers, "choices": choices, "clarify_rounds": rounds + 1, "route": "understand"}
        # Refresh RAG after the stakeholder's intent and values are known, including after clarification.
        if self.features.retrieval:
            query = " ".join([frame.restated_question, *frame.measures,
                              *(f"{v.name} {f.text} {f.value}" for v in res.subjects for f in v.filters),
                              *(c for v in res.subjects for c, _ in v.breakdowns)])
            context = self.retriever.retrieve(query, set(state.get("exclude_examples") or []))
            needed = res.tables()
            for view in res.subjects:
                needed += [step.right for step in self.joins.path(view.table, [t for t in res.tables()
                                                                            if t != view.table])]
            known_metrics = [m for m in frame.measures if m in self.metrics.metrics or m in self.metrics.custom_metrics]
            for metric in known_metrics:
                needed += self.retriever._metric_tables(metric)
            context.tables = list(dict.fromkeys([*needed, *context.tables]))
            context.metrics = list(dict.fromkeys([*known_metrics, *context.metrics]))
            context.joins = [j for j in self.retriever.schema.joins
                             if all(side.strip().split(".")[0] in context.tables for side in j.split("="))]
            refreshed = {"context": context.to_prompt(self.retriever.schema, self.metrics),
                         "retrieved": {"tables": context.tables, "metrics": context.metrics, "joins": context.joins,
                                       "values": [[v.value, v.columns, v.score] for v in context.values],
                                       "examples": [e.id for e in context.examples], "terms": context.terms},
                         "metric_names": context.metrics}
        else:
            refreshed = {}
        text = res.to_prompt(period=frame.period)
        filters = [[f.column, f.value] for v in res.subjects for f in v.filters] + \
                  [[i.column, i.value] for v in res.subjects for i in v.id_filters]
        columns = {c for v in res.subjects for c in [v.time_column, *(c for _, c in v.breakdowns),
                                                    *(f.column for f in v.filters)]}
        wanted = set(frame.measures) | set(state.get("metric_names") or [])
        shown = set((state.get("retrieved") or {}).get("examples") or [])
        refs = self.references.pick(set(res.tables()), columns, wanted,
                                    exclude=set(state.get("exclude_examples") or []) | shown)
        scope = None
        if len(res.subjects) == 1:                         # one subject: its metrics can be recomputed for the scope
            v = res.subjects[0]
            dates = re.findall(r"\d{4}-\d{2}-\d{2}", frame.period or "")
            period_valid = frame.period.strip().lower() == "all data"
            if len(dates) == 2:
                try:
                    period_valid = date.fromisoformat(dates[0]) <= date.fromisoformat(dates[1])
                except ValueError:
                    period_valid = False
            scope = {"table": v.table, "metrics": [m for m in frame.measures if m in self.metrics.metrics],
                     "by": {c.split(".")[1]: c for _, c in v.breakdowns},
                     "filters": [[f.column, f.value] for f in v.filters] + [[i.column, i.value] for i in v.id_filters],
                     "period": dates if len(dates) == 2 else None, "time": v.time_column,
                     "supported": period_valid and not frame.comparison, "ranking": frame.ranking,
                     "limit": (frame.row_limit or 10) if frame.ranking else None}
        out = {**refreshed, "frame": frame.model_dump(), "understanding": text, "understood_tables": res.tables(),
               "verify_scope": scope,
               "resolved_filters": filters, "references": self.references.render(refs),
               "answers": answers, "choices": choices, "open_points": [], "warnings": warnings, "route": "plan"}
        detail = {"understood": frame.restated_question, "rounds": rounds, "subjects": [v.name for v in res.subjects],
                  "notes": res.notes, "resolved": text, "references": [e.id for e in refs], "verify_scope": scope}
        # read back what will be computed and wait for the user's confirmation (defaults: confirmed automatically)
        if not state.get("use_defaults") and not state.get("confirmed") and res.subjects:
            if rounds < MAX_CLARIFY_ROUNDS:
                point = OpenPoint("confirm", CONFIRM_QUESTION, [CONFIRM_YES, CONFIRM_NO], CONFIRM_YES,
                                  "nothing is computed until you confirm", {CONFIRM_YES: "yes"})
                summary = readback(frame, res, self.concepts, self.metrics)
                return {**out, "open_points": [point.__dict__], "readback": summary, "route": "clarify",
                        "_status": "needs_input", "_detail": {**detail, "round": rounds + 1, "readback": summary}}
            return {**out, "status": "declined", "route": "finish",
                    "message": "The clarification limit was reached before confirmation. Please restate the question."}
        return {**out, "_detail": detail}

    @step("plan")
    def plan(self, state: State) -> dict:
        if not self.features.planner:                      # ablation: no planning, question goes to the writer
            plan = agents.AnalysisPlan(decision="proceed", restated_question=state["question"])
            return {"plan": plan.model_dump(), "route": "write_sql", "_detail": {"decision": "skipped"}}
        result = agents.plan(self.llm(state), state["question"], state["context"], state["memory"], self.data,
                             state.get("history", ""), state.get("answers"), state.get("use_defaults", False),
                             state.get("understanding", ""))
        detail = {"decision": result.decision, "plan": agents.plan_text(result)}
        if result.decision == "decline":
            return {"plan": result.model_dump(), "status": "declined", "route": "finish", "_detail": detail,
                    "message": result.decline_reason or "This question can't be answered from the shop's data."}
        if result.decision == "clarify" and result.clarifying_questions:
            if state.get("clarify_rounds", 0) >= MAX_CLARIFY_ROUNDS:
                return {"status": "declined", "route": "finish",
                        "message": "The analysis still needs clarification; no query was executed."}
            return {"plan": result.model_dump(), "route": "clarify", "_status": "needs_input", "_detail": detail}
        return {"plan": result.model_dump(), "route": "write_sql", "_detail": detail}

    def clarify(self, state: State) -> dict:
        """Pause for the user's answers, then go back to wherever the questions came from."""
        if state.get("open_points"):                                        # from the understanding step
            points = [OpenPoint(**p) for p in state["open_points"]]
            questions = [p.public() for p in points]
        else:                                                               # planner (understanding disabled)
            points, questions = [], state["plan"]["clarifying_questions"]
        rounds = state.get("clarify_rounds", 0) + 1
        confirming = any(p.key == "confirm" for p in points)
        payload = {"type": "clarify", "kind": "confirm" if confirming else "clarify", "questions": questions,
                   "round": rounds}
        if confirming:
            payload["readback"] = state.get("readback", "")
        given = interrupt(payload)                                          # pauses the run here
        if not isinstance(given, dict) or not given:                        # "just use defaults"
            given = {q["question"]: q["default"] for q in questions}
        if confirming:
            answer = (given.get(CONFIRM_QUESTION) or CONFIRM_YES).strip()
            if answer == CONFIRM_YES:                                       # confirmed: plan with what was resolved
                return {"confirmed": True, "clarify_rounds": rounds, "open_points": [], "status": "running",
                        "route": "plan"}
            correction = "The user wants something changed but did not say what: ask what to change" \
                if answer == CONFIRM_NO else answer
            answers = {**(state.get("answers") or {}), CORRECTION: correction}
            return {"answers": answers, "confirmed": False, "clarify_rounds": rounds, "open_points": [],
                    "status": "running", "route": "understand"}
        answers = {**(state.get("answers") or {}), **given}
        choices = dict(state.get("choices") or {})
        for p in points:
            answer = given.get(p.question, p.default)
            if p.meaning and answer in p.meaning:                           # a choice code can act on
                choices[p.key] = p.meaning[answer]
        return {"answers": answers, "choices": choices, "clarify_rounds": rounds, "open_points": [],
                "confirmed": False, "status": "running",
                "route": "understand" if self.features.understand else "plan"}

    def narrowed_context(self, state: State, plan: agents.AnalysisPlan) -> tuple[str, list[str]]:
        """Context limited to the plan's source tables (less distraction for the SQL writer).
        Falls back to the full context when the plan names no valid tables."""
        from .retrieval import Context, Value

        r = state.get("retrieved") or {}
        tables = [t for t in dict.fromkeys(plan.source_tables + list(state.get("understood_tables") or []))
                  if t in self.retriever.schema.tables]
        if not r or not tables:
            return state["context"], list(r.get("tables", []))
        keep = set(tables)
        joins = [j for j in self.retriever.schema.joins
                 if all(side.strip().split(".")[0] in keep for side in j.split("="))]
        values = [Value(v, [c for c in cols if c.split(".")[0] in keep], sc) for v, cols, sc in r["values"]]
        examples = [self.retriever.examples[i] for i in r["examples"] if i in self.retriever.examples]
        examples = [e for e in examples if tables_in(e.sql) & keep] or examples[:1]
        ctx = Context(state["question"], tables, r["metrics"], examples, [v for v in values if v.columns], joins,
                      r.get("terms", {}))
        return ctx.to_prompt(self.retriever.schema, self.metrics), tables

    @step("write_sql")
    def write_sql(self, state: State) -> dict:
        plan = agents.AnalysisPlan.model_validate(state["plan"])
        if (get_settings().enable_sql_compiler and self.features.understand
                and not state.get("attempts") and state.get("verify_scope")):
            from .sql_compiler import compile_standard

            compiled = compile_standard(state["verify_scope"], self.joins)
            if compiled is not None:
                return {"sql": compiled, "explanation": "Deterministic metric SQL from the semantic layer",
                        "attempts": 1, "feedback": [],
                        "_detail": {"attempt": 1, "sql": compiled,
                                    "explanation": "Compiled without an LLM; metric and joins are code-defined.",
                                    "context_tables": state.get("understood_tables") or []}}
        first = state.get("attempts", 0) == 0 and self.features.narrow
        # first attempt: only the plan's tables; retries get the full context so recovery stays possible
        context, tables = self.narrowed_context(state, plan) if first else (
            state["context"], list((state.get("retrieved") or {}).get("tables", [])))
        draft = agents.write_sql(self.llm(state), state["question"], plan, context,
                                 state.get("feedback") or None, state.get("sql"), state.get("understanding", ""),
                                 state.get("references", ""))
        attempt = state.get("attempts", 0) + 1
        return {"sql": draft.sql.strip().rstrip(";"), "explanation": draft.explanation, "attempts": attempt,
                "feedback": [], "_detail": {"attempt": attempt, "sql": draft.sql.strip(),
                                            "explanation": draft.explanation, "context_tables": tables}}

    def _retry_or_fail(self, state: State, problems: list[str], step_name: str) -> dict:
        if state.get("attempts", 0) < get_settings().max_sql_attempts:
            return {"feedback": problems, "route": "write_sql", "_status": "retry", "_detail": {"problems": problems}}
        return {"status": "failed", "route": "finish", "_status": "failed", "_detail": {"problems": problems},
                "message": f"Could not produce a valid query after {state.get('attempts', 0)} attempts "
                           f"({step_name}): " + "; ".join(problems)}

    @step("check_sql")
    def check_sql(self, state: State) -> dict:
        if not (state.get("sql") or "").strip():          # the writer declined or ran out of room: say so
            reason = (state.get("explanation") or "").strip()[:300] or "no reason given"
            return self._retry_or_fail(state, [
                f"No SQL was returned (writer's note: {reason}). Return exactly one SELECT statement in the "
                "sql field. Aggregates by region, city or tier, and questions about email or SMS campaigns, "
                "are allowed; only the hidden personal columns are unavailable."], "safety checks")
        verdict = check_sql(state["sql"])
        if not verdict.ok:
            return self._retry_or_fail(state, verdict.errors, "safety checks")
        return {"sql": verdict.sql, "route": "run_query", "_detail": {"tables": sorted(verdict.tables)}}

    @step("run_query")
    def run_query(self, state: State) -> dict:
        if self.snapshot and snapshot_signature() != self.snapshot:
            return {"status": "failed", "route": "finish",
                    "message": "The data was refreshed while this question was open. Please ask again against "
                               "the new snapshot; the old interpretation was not executed."}
        try:
            result = run_query(self.con, state["sql"])
        except QueryTimeout as error:
            return self._retry_or_fail(state, [f"{error}. Simplify the query or aggregate earlier."], "database")
        except Exception as error:                                          # DuckDB error: let the writer fix it
            return self._retry_or_fail(state, [f"Database error: {str(error).splitlines()[0][:300]}"], "database")
        # code checks first (filters used, no repeated rows, no empty columns, rates within 0-100%); 0 rows is not
        # automatically wrong (e.g. no orders before launch): the validator decides
        problems = result_problems(result.frame, state["sql"], [tuple(f) for f in state.get("resolved_filters") or []])
        if problems:
            return self._retry_or_fail(state, problems, "result checks")
        state_warnings = state.get("warnings", [])
        verification = self.verify_numbers(state, result.frame)
        if verification["status"] == "mismatch":
            return self._retry_or_fail(state, verification["problems"], "number check")
        return {"result": _result(result.frame, result.truncated), "warnings": state_warnings,
                "verification": {k: v for k, v in verification.items() if k != "problems"},
                "route": "validate" if self.features.validator else "analyse",
                "_detail": {"rows": len(result.frame), "columns": list(result.frame.columns),
                            "query_seconds": round(result.seconds, 2), "truncated": result.truncated}}

    def verify_numbers(self, state: State, frame: pd.DataFrame) -> dict:
        """Recompute each confirmed metric from metrics.yaml for the confirmed scope and compare (recompute.py)."""
        scope = state.get("verify_scope")
        if not self.features.understand or not scope or not scope.get("metrics") or not scope.get("supported", True):
            return {"status": "unverified", "metrics": {}, "problems": []}
        statuses, problems = {}, []
        for metric in scope["metrics"]:
            m = self.metrics.metrics[metric]
            if self.metrics.sources[m.source].from_.split()[0] != scope["table"]:
                statuses[metric] = "unverified"                # another subject's metric: not this scope
                continue
            period = tuple(scope["period"]) if scope.get("period") else None
            sql = canonical_sql(metric, self.joins, by=scope["by"], filters=[tuple(f) for f in scope["filters"]],
                                period=period, time_column=scope["time"])
            if sql is None:
                statuses[metric] = "unverified"
                continue
            try:
                canonical = run_query(self.con, sql, max_rows=100_000).frame
            except Exception:                                    # never fail an answer because a check failed
                statuses[metric] = "unverified"
                continue
            status, found = check_answer(frame, canonical, list(scope["by"]), ratio=m.unit == "ratio", column=metric,
                                         limit=scope.get("limit"), ranking=scope.get("ranking"))
            statuses[metric] = status
            problems += [f"{m.label}: {p}" for p in found]
        values = set(statuses.values())
        overall = "mismatch" if "mismatch" in values else "verified" if values == {"verified"} else "unverified"
        return {"status": overall, "metrics": statuses, "problems": problems}

    @step("validate")
    def validate(self, state: State) -> dict:
        plan = agents.AnalysisPlan.model_validate(state["plan"])
        verdict = agents.validate(self.llm(state), state["question"], plan, state["sql"], _frame(state["result"]),
                                  self.metrics.describe(state.get("metric_names") or None),
                                  state.get("understanding", ""))
        if verdict.ok:
            return {"route": "analyse", "_detail": {"ok": True}}
        if state.get("attempts", 0) < get_settings().max_sql_attempts:
            return {"feedback": verdict.problems, "route": "write_sql", "_status": "retry",
                    "_detail": {"ok": False, "problems": verdict.problems}}
        return self._retry_or_fail(state, verdict.problems, "reviewer")

    @step("analyse")
    def analyse(self, state: State) -> dict:
        plan = agents.AnalysisPlan.model_validate(state["plan"])
        frame = _frame(state["result"])
        notes = "\n".join(f"- {n}: {self.metrics.metrics[n].notes}" for n in plan.metrics
                          if n in self.metrics.metrics and self.metrics.metrics[n].notes)
        args = (self.llm(state), state["question"], plan, state["sql"], frame, state["memory"], notes, self.data)
        concerns = list(state.get("warnings", []))
        if (state.get("verification") or {}).get("status") != "verified":
            concerns.append("These metrics could not be independently verified against the confirmed scope.")
        if state["result"].get("truncated"):
            concerns.append("The result is truncated; do not treat partial rows as the complete population.")
        analysis = agents.analyse(*args, concerns=concerns)
        check = verify(frame, analysis.claims, analysis.texts())
        rewrote = False
        if not check.ok:                                   # one rewrite with the exact problems
            analysis = agents.analyse(*args, fix_numbers=check.problems, concerns=concerns)
            rewrote = True
            check = verify(frame, analysis.claims, analysis.texts())
        warnings = list(state.get("warnings", []))
        if not check.ok:
            warnings.append("Unverified claims: " + "; ".join(check.problems[:6]))
        shown = analysis.rendered()
        return {"analysis": shown.model_dump(), "grounded": check.ok, "warnings": warnings,
                "route": "chart" if check.ok else "finish", "status": "running" if check.ok else "failed",
                "message": None if check.ok else "The answer's claims could not be verified after a rewrite.",
                "_detail": {"headline": shown.headline, "number_check": "passed" if check.ok else "unverified",
                            "claims": check.checked, "rewritten": rewrote, "unverified": check.problems[:6]}}

    @step("chart")
    def chart(self, state: State) -> dict:
        a = state["analysis"]
        chart = build_chart(_frame(state["result"]), a.get("chart_title") or a["headline"],
                            a.get("chart_subtitle", ""), a.get("chart_metric"))
        return {"chart": chart, "_detail": {"kind": (chart or {}).get("kind")}}

    @step("remember")
    def remember(self, state: State) -> dict:
        a = state["analysis"]
        memory = self.mem(state)
        if self.snapshot and snapshot_signature() != self.snapshot:
            return {"insight_id": None, "status": "failed",
                    "message": "The data changed during analysis. Please ask again; no insight was saved."}
        if not self.features.memory:
            return {"insight_id": None, "status": "done", "_status": "skipped",
                    "_detail": {"saved": False, "reason": "memory disabled"}}
        if self.features.understand and (state.get("verification") or {}).get("status") != "verified":
            return {"insight_id": None, "status": "done", "_status": "skipped",
                    "_detail": {"saved": False, "reason": "not independently verified; not saved to memory"}}
        concerns = [w for w in state.get("warnings", []) if w.startswith(
            ("Reviewer concern", "Result check", "Numbers not confirmed"))]
        if (state.get("verification") or {}).get("status") == "mismatch":
            concerns.append("canonical metric mismatch")
        if (state.get("result") or {}).get("truncated"):
            concerns.append("truncated result")
        if concerns or state.get("grounded") is not True:
            reason = "validator raised concerns" if concerns else "claims did not verify"
            return {"insight_id": None, "status": "done", "_status": "skipped",
                    "_detail": {"saved": False, "reason": f"not saved to memory: {reason}"}}
        item = memory.save_insight(question=state["question"], plan=state["plan"], sql=state["sql"],
                                   analysis=a, data_as_of=self.data["last_day"], chat_id=state.get("chat_id"))
        closed = memory.close_gaps(a.get("explains_gap_ids", []), item["id"], self.data["last_day"])
        return {"insight_id": item["id"], "status": "done",
                "_detail": {"facts": len(a.get("memory", [])), "gaps_closed": closed}}

    def finish(self, state: State) -> dict:
        return {}

    # ------------------------------------------------------------------ graph
    def _build(self) -> StateGraph:
        g = StateGraph(State)
        for name in ["check_input", "retrieve", "understand", "plan", "clarify", "write_sql", "check_sql", "run_query",
                     "validate", "analyse", "chart", "remember", "finish"]:
            g.add_node(name, getattr(self, name))
        g.add_edge(START, "check_input")
        route = lambda s: s["route"]                                       # noqa: E731
        g.add_conditional_edges("check_input", route, ["retrieve", "finish"])
        g.add_edge("retrieve", "understand")
        g.add_conditional_edges("understand", route, ["clarify", "plan", "understand", "finish"])
        g.add_conditional_edges("plan", route, ["clarify", "write_sql", "finish"])
        g.add_conditional_edges("clarify", route, ["understand", "plan"])
        g.add_edge("write_sql", "check_sql")
        for name in ["check_sql", "run_query"]:
            g.add_conditional_edges(name, route, ["write_sql", "run_query", "validate", "analyse", "finish"])
        g.add_conditional_edges("validate", route, ["write_sql", "analyse", "finish"])
        g.add_conditional_edges("analyse", route, ["chart", "finish"])
        g.add_edge("chart", "remember")
        g.add_edge("remember", "finish")
        g.add_edge("finish", END)
        return g

    # ------------------------------------------------------------------ running
    def start(self, question: str, chat_id: str | None = None, history: str = "",
              use_defaults: bool = False, memory: Any = None,
              exclude_examples: list[str] | None = None) -> tuple[str, Iterator[dict]]:
        """memory: a scope with context(), save_insight() and close_gaps(); defaults to the file memory."""
        run_id = uuid.uuid4().hex[:12]
        if memory is not None:
            self._memories[run_id] = memory
        state = {"run_id": run_id, "chat_id": chat_id, "question": question, "history": history,
                 "use_defaults": use_defaults, "exclude_examples": list(exclude_examples or [])}
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
                    yield {"type": "clarify", "run_id": run_id, **chunk["__interrupt__"][0].value}
                    return
        except BudgetExceeded as error:
            final = self.final(run_id)
            yield {**final, "status": "failed", "message": str(error)}
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
        return {"type": "final", "run_id": run_id, "status": state.get("status"), "message": state.get("message"),
                "question": state.get("question"), "plan": state.get("plan"), "sql": state.get("sql"),
                "result": state.get("result"), "analysis": state.get("analysis"), "chart": state.get("chart"),
                "warnings": state.get("warnings", []), "insight_id": state.get("insight_id"),
                "grounded": state.get("grounded"), "verification": state.get("verification"),
                "attempts": state.get("attempts"), "data_as_of": self.data["last_day"],
                "data_size": self.data["size"], "cost": llm.tracker.summary() if llm else None,
                "calls": [c.__dict__ for c in llm.tracker.calls] if llm else []}


# ----------------------------------------------------------------------------- command line
def _print_event(event: dict) -> None:
    if event.get("type") != "step":
        return
    detail = event.get("detail", {})
    mark = {"started": "…", "done": "✓", "retry": "↻", "failed": "✗", "warning": "!", "blocked": "✗",
            "needs_input": "?"}.get(event["status"], "·")
    if event["status"] == "started":
        return
    extra = ""
    if event["step"] == "retrieve":
        extra = f" tables={detail.get('tables')} values={detail.get('values')}"
    elif event["step"] == "understand":
        extra = f" {detail.get('understood', '')}" + (f"\n    asking: {detail['questions']}" if detail.get("questions")
                                                     else "")
    elif event["step"] == "plan":
        extra = "\n    " + detail.get("plan", "").replace("\n", "\n    ")
    elif event["step"] == "write_sql":
        extra = f" (attempt {detail.get('attempt')})\n    " + detail.get("sql", "").replace("\n", "\n    ")
    elif detail.get("problems"):
        extra = " " + "; ".join(detail["problems"])
    elif event["step"] == "run_query":
        extra = f" {detail.get('rows')} rows"
    print(f"  {mark} {event['step']} {detail.get('seconds', '')}s{extra}")


def main() -> None:
    """pantree-ask [--defaults] "question": ask the agent from the terminal."""
    import argparse

    parser = argparse.ArgumentParser(prog="pantree-ask")
    parser.add_argument("question", nargs="+")
    parser.add_argument("--defaults", action="store_true", help="never ask clarifying questions")
    args = parser.parse_args()
    question = " ".join(args.question)
    agent = Agent()
    print(f"\n{question}\n")
    run_id, events = agent.start(question, use_defaults=args.defaults)
    while True:
        final = None
        for event in events:
            _print_event(event)
            if event["type"] in ("clarify", "final"):
                final = event
        if final and final["type"] == "clarify":
            answers = {}
            if final.get("readback"):
                print("\nHere's what I'll compute:\n  " + final["readback"].replace("**", "").replace("\n", "\n  "))
            print(f"\nQuestions, round {final.get('round', 1)} (press Enter for the default, or type your own answer):")
            for q in final["questions"]:
                options = " / ".join(q["options"])
                answer = input(f"  {q['question']} [{options}] (default: {q['default']}): ").strip()
                answers[q["question"]] = answer or q["default"]
            events = agent.resume(run_id, answers)
            continue
        break
    if final["status"] != "done":
        print(f"\n{final['status'].upper()}: {final.get('message')}")
        return
    a = final["analysis"]
    print(f"\n{a['headline']}\n")
    for f in a["findings"]:
        print(f"  • {f}")
    if a.get("recommendations"):
        print("\nRecommendations:")
        for r in a["recommendations"]:
            print(f"  → {r}")
    if a.get("caveats") or final["warnings"]:
        print("\nCaveats:")
        for c in a.get("caveats", []) + final["warnings"]:
            print(f"  ! {c}")
    cost = final.get("cost") or {}
    print(f"\nChart: {(final.get('chart') or {}).get('kind')} | data as of {final['data_as_of']} | "
          f"{cost.get('calls')} LLM calls, {cost.get('input')} in / {cost.get('output')} out tokens, "
          f"${cost.get('usd', 0):.4f}")
