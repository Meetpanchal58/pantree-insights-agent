"""Graph: nodes execute."""
from __future__ import annotations

import contextlib
import time

from langgraph.config import get_stream_writer

from ..db import snapshot_signature
from ..interpret import readback
from ..spec import QuerySpec, SpecError
from ..templates import TemplateError
from .state import CONFIRM_QUESTION, OpenPoint, State, _result, log, plan_digest, step


class ExecuteNodes:
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
            from ..investigate import MAX_QUERIES
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

    @step("reflect")
    def reflect(self, state: State) -> dict:
        from ..investigate import MAX_CALLS, MAX_QUERIES, MAX_ROUNDS, MAX_USD, resolve_probe, retain_scope, review
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
