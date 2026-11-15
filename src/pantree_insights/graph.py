"""The agent flow, as a LangGraph state machine.

    check_input -> retrieve -> plan --(clarify: pause for the user's answers)--> plan
                                    \\-(decline)-> finish
                               plan -> write_sql -> check_sql -> run_query -> validate -> analyse
                                          ^________|___________|____________|   (feedback, max 3 tries)
                               analyse -> (number check, one rewrite) -> chart -> remember -> finish

Every step streams progress events ({"type": "step", "step", "status", "detail", ...}) so the UI can
show the agents working. A run pauses at clarifying questions and resumes with Agent.resume().
"""
from __future__ import annotations

import contextlib
import time
import uuid
from collections.abc import Callable, Iterator
from functools import wraps
from typing import Any, TypedDict

import pandas as pd
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from . import agents
from .charts import build_chart
from .config import get_settings
from .db import QueryTimeout, connect, manifest, run_query
from .grounding import verify
from .guardrails import check_question, check_sql
from .llm import LLM, BudgetExceeded, ChatLLM
from .memory import Memory
from .retrieval import Retriever, get_retriever
from .semantic import load_metrics, tables_in


class State(TypedDict, total=False):
    run_id: str
    chat_id: str | None
    question: str
    history: str
    use_defaults: bool
    exclude_examples: list[str]  # evaluation: hide these example ids from retrieval (no leakage)
    answers: dict[str, str]
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
                 memory: Memory | None = None, con=None, data: dict | None = None):
        settings = get_settings()
        self.llm_factory = llm_factory or ChatLLM
        self.con = con or connect(settings.shop_dir)
        self.retriever = retriever or get_retriever()
        self.memory = memory or Memory()
        self.metrics = load_metrics()
        info = data or manifest(settings.shop_dir)
        self.data = {"first_day": info["first_day"], "last_day": info["last_day"], "size": info["size"]}
        self._llms: dict[str, LLM] = {}
        self._memories: dict[str, Any] = {}          # run_id -> memory scope for that question
        self.graph = self._build().compile(checkpointer=InMemorySaver())

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
        context = self.retriever.retrieve(state["question"], set(state.get("exclude_examples") or []))
        retrieved = {"tables": context.tables, "metrics": context.metrics, "joins": context.joins,
                     "values": [[v.value, v.columns, v.score] for v in context.values],
                     "examples": [e.id for e in context.examples], "terms": context.terms}
        return {"context": context.to_prompt(self.retriever.schema, self.metrics), "retrieved": retrieved,
                "metric_names": context.metrics,
                "memory": self.mem(state).context(state["question"], as_of=self.data["last_day"]),
                "_detail": {"tables": context.tables, "metrics": context.metrics,
                            "values": [v.value for v in context.values[:6]],
                            "examples": [e.question for e in context.examples[:3]]}}

    @step("plan")
    def plan(self, state: State) -> dict:
        result = agents.plan(self.llm(state), state["question"], state["context"], state["memory"], self.data,
                             state.get("history", ""), state.get("answers"), state.get("use_defaults", False))
        detail = {"decision": result.decision, "plan": agents.plan_text(result)}
        if result.decision == "decline":
            return {"plan": result.model_dump(), "status": "declined", "route": "finish", "_detail": detail,
                    "message": result.decline_reason or "This question can't be answered from the shop's data."}
        if result.decision == "clarify" and result.clarifying_questions:
            return {"plan": result.model_dump(), "route": "clarify", "_status": "needs_input", "_detail": detail}
        return {"plan": result.model_dump(), "route": "write_sql", "_detail": detail}

    def clarify(self, state: State) -> dict:
        questions = state["plan"]["clarifying_questions"]
        answers = interrupt({"type": "clarify", "questions": questions})     # pauses the run here
        if not isinstance(answers, dict) or not answers:                    # "just use defaults"
            answers = {q["question"]: q["default"] for q in questions}
        return {"answers": answers, "status": "running"}

    def narrowed_context(self, state: State, plan: agents.AnalysisPlan) -> tuple[str, list[str]]:
        """Context limited to the plan's source tables (less distraction for the SQL writer).
        Falls back to the full context when the plan names no valid tables."""
        from .retrieval import Context, Value

        r = state.get("retrieved") or {}
        tables = [t for t in dict.fromkeys(plan.source_tables) if t in self.retriever.schema.tables]
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
        first = state.get("attempts", 0) == 0
        # first attempt: only the plan's tables; retries get the full context so recovery stays possible
        context, tables = self.narrowed_context(state, plan) if first else (
            state["context"], list((state.get("retrieved") or {}).get("tables", [])))
        draft = agents.write_sql(self.llm(state), state["question"], plan, context,
                                 state.get("feedback") or None, state.get("sql"))
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
        verdict = check_sql(state["sql"])
        if not verdict.ok:
            return self._retry_or_fail(state, verdict.errors, "safety checks")
        return {"sql": verdict.sql, "route": "run_query", "_detail": {"tables": sorted(verdict.tables)}}

    @step("run_query")
    def run_query(self, state: State) -> dict:
        try:
            result = run_query(self.con, state["sql"])
        except QueryTimeout as error:
            return self._retry_or_fail(state, [f"{error}. Simplify the query or aggregate earlier."], "database")
        except Exception as error:                                          # DuckDB error: let the writer fix it
            return self._retry_or_fail(state, [f"Database error: {str(error).splitlines()[0][:300]}"], "database")
        # 0 rows is not automatically wrong (e.g. no orders before launch): the validator decides
        return {"result": _result(result.frame, result.truncated), "route": "validate",
                "_detail": {"rows": len(result.frame), "columns": list(result.frame.columns),
                            "query_seconds": round(result.seconds, 2), "truncated": result.truncated}}

    @step("validate")
    def validate(self, state: State) -> dict:
        plan = agents.AnalysisPlan.model_validate(state["plan"])
        verdict = agents.validate(self.llm(state), state["question"], plan, state["sql"], _frame(state["result"]),
                                  self.metrics.describe(state.get("metric_names") or None))
        if verdict.ok:
            return {"route": "analyse", "_detail": {"ok": True}}
        if state.get("attempts", 0) < get_settings().max_sql_attempts:
            return {"feedback": verdict.problems, "route": "write_sql", "_status": "retry",
                    "_detail": {"ok": False, "problems": verdict.problems}}
        warnings = state.get("warnings", []) + [f"Reviewer concern: {p}" for p in verdict.problems]
        return {"warnings": warnings, "route": "analyse", "_status": "warning",
                "_detail": {"ok": False, "problems": verdict.problems}}

    @step("analyse")
    def analyse(self, state: State) -> dict:
        plan = agents.AnalysisPlan.model_validate(state["plan"])
        frame = _frame(state["result"])
        notes = "\n".join(f"- {n}: {self.metrics.metrics[n].notes}" for n in plan.metrics
                          if n in self.metrics.metrics and self.metrics.metrics[n].notes)
        args = (self.llm(state), state["question"], plan, state["sql"], frame, state["memory"], notes, self.data)
        analysis = agents.analyse(*args)
        check = verify(frame, analysis.claims, analysis.texts())
        rewrote = False
        if not check.ok:                                   # one rewrite with the exact problems
            analysis = agents.analyse(*args, fix_numbers=check.problems)
            rewrote = True
            check = verify(frame, analysis.claims, analysis.texts())
        warnings = list(state.get("warnings", []))
        if not check.ok:
            warnings.append("Unverified claims: " + "; ".join(check.problems[:6]))
        shown = analysis.rendered()
        return {"analysis": shown.model_dump(), "grounded": check.ok, "warnings": warnings,
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
        concerns = [w for w in state.get("warnings", []) if w.startswith("Reviewer concern")]
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
        for name in ["check_input", "retrieve", "plan", "clarify", "write_sql", "check_sql", "run_query",
                     "validate", "analyse", "chart", "remember", "finish"]:
            g.add_node(name, getattr(self, name))
        g.add_edge(START, "check_input")
        route = lambda s: s["route"]                                       # noqa: E731
        g.add_conditional_edges("check_input", route, ["retrieve", "finish"])
        g.add_edge("retrieve", "plan")
        g.add_conditional_edges("plan", route, ["clarify", "write_sql", "finish"])
        g.add_edge("clarify", "plan")
        g.add_edge("write_sql", "check_sql")
        for name in ["check_sql", "run_query"]:
            g.add_conditional_edges(name, route, ["write_sql", "run_query", "validate", "finish"])
        g.add_conditional_edges("validate", route, ["write_sql", "analyse"])
        g.add_edge("analyse", "chart")
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
        config = {"configurable": {"thread_id": run_id}}
        try:
            for mode, chunk in self.graph.stream(payload, config, stream_mode=["custom", "updates"]):
                if mode == "custom":
                    yield chunk
                elif "__interrupt__" in chunk:
                    yield {"type": "clarify", "run_id": run_id, **chunk["__interrupt__"][0].value}
                    return
        except BudgetExceeded as error:
            yield {"type": "final", "run_id": run_id, "status": "failed", "message": str(error)}
            return
        yield self.final(run_id)

    def final(self, run_id: str) -> dict:
        state = self.graph.get_state({"configurable": {"thread_id": run_id}}).values
        llm = self._llms.pop(run_id, None)
        self._memories.pop(run_id, None)
        return {"type": "final", "run_id": run_id, "status": state.get("status"), "message": state.get("message"),
                "question": state.get("question"), "plan": state.get("plan"), "sql": state.get("sql"),
                "result": state.get("result"), "analysis": state.get("analysis"), "chart": state.get("chart"),
                "warnings": state.get("warnings", []), "insight_id": state.get("insight_id"),
                "grounded": state.get("grounded"),
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
            print("\nThe planner has questions (press Enter for the default):")
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
