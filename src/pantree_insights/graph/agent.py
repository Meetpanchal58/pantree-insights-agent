"""Graph: agent."""
from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterator
from typing import Any

import pandas as pd
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from .. import agents
from ..catalog import label_columns, load_catalog
from ..config import get_settings
from ..context import populations
from ..db import connect, manifest, run_query, snapshot_signature
from ..interpret import readback
from ..joins import JoinGraph
from ..llm import LLM, BudgetExceeded, ChatLLM
from ..memory import Memory
from ..profile import profile_for
from ..semantic import load_concepts, load_glossary, load_metrics, load_schema
from ..spec import Catalog, QuerySpec
from .nodes_deliver import DeliverNodes
from .nodes_execute import ExecuteNodes
from .nodes_interpret import InterpretNodes
from .state import CACHE_SIZE, State, log


class Agent(InterpretNodes, ExecuteNodes, DeliverNodes):
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

    def start(self, question: str, chat_id: str | None = None, history: str = "", use_defaults: bool = False,
              memory: Any = None, run_id: str | None = None,
              previous_spec: dict | None = None, investigation: dict | None = None,
              initial_answers: dict[str, str] | None = None) -> tuple[str, Iterator[dict]]:
        """memory: a scope with context(), save_insight() and close_gaps(); defaults to the file memory."""
        run_id = run_id or uuid.uuid4().hex[:12]
        if memory is not None:
            self._memories[run_id] = memory
        state = {"run_id": run_id, "chat_id": chat_id, "question": question, "history": history,
                 "previous_spec": previous_spec, "plan_version": 0, "investigation": investigation or {},
                 "answers": initial_answers or {}}
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
