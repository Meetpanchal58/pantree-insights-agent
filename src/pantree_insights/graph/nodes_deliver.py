"""Graph: nodes deliver."""
from __future__ import annotations

from .. import agents
from ..charts import build_chart
from ..db import snapshot_signature
from ..grounding import verify
from ..spec import QuerySpec
from ..templates import RESULT_UNITS
from .analysis import deterministic_analysis, drop_unverified
from .state import State, _frame, step


class DeliverNodes:
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
