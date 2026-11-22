"""Graph: nodes interpret."""
from __future__ import annotations

import json
import re
import time

from langgraph.types import interrupt

from .. import agents
from ..config import get_settings
from ..fastpath import followup, independent_history
from ..fastpath import parse as fast_spec
from ..guardrails import check_question, check_sql
from ..interpret import READINGS, Decision, TemplateCall, decide, readback
from ..spec import QuerySpec, SpecError, compile_spec, validate
from ..templates import TemplateError
from ..templates import build as build_template
from .state import CONFIRM_QUESTION, CORRECTION, MAX_CLARIFY_ROUNDS, OpenPoint, State, _emit, plan_digest, step


class InterpretNodes:
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
            from ..interpret import revise, revise_template
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
                    "memory": background, "revision": "", "clarification_notice": "",
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
        from ..interpret import coverage_problems
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
        from ..interpret import is_confirmation
        points = [OpenPoint(**p) for p in state.get("open_points") or []]
        confirming = bool(state.get("prepared")) and any(p.key == "confirm" for p in points)
        version = state.get("plan_version", 0)
        text = ("I understand you want to " + (state.get("restated") or state["question"]).rstrip(".?") +
                ".\n\n" + state.get("readback", "") +
                "\n\nDoes this match your objective? Say ‘go ahead’ to run this plan, or tell me what to change.") \
            if confirming else "\n\n".join(p.question + (" Possible meanings: " + "; ".join(p.options)
                                                          if p.options else "") for p in points)
        if state.get("clarification_notice"):
            text = state["clarification_notice"] + "\n\n" + text
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
            if state.get("prepared") and all(p.get("sql") for p in state["prepared"]):
                # Recover from an unresolved edit by re-presenting the intact plan, never executing implicitly.
                prepared = self.prepare(state)
                version += 1
                summary = "\n\n".join(readback(QuerySpec.model_validate(p["spec"]), self.catalog)
                    if p.get("spec") else build_template(p["template"]["name"],
                        TemplateCall.model_validate(p["template"]).params)[1] for p in prepared)
                return {"route": "clarify", "prepared": prepared, "readback": summary,
                        "plan_version": version, "plan_digest": plan_digest(prepared, version, self.snapshot),
                        "confirmed_digest": None, "revision": "", "clarification_notice":
                        "No correction was applied. Please confirm the unchanged plan below.",
                        "open_points": [OpenPoint("confirm", CONFIRM_QUESTION, [], "", "Confirm the plan").__dict__]}
            return {"route": "clarify", "confirmed_digest": None, "clarification_notice":
                    "Please answer the open question below before I can prepare a plan."}
        if confirming and len(message.strip()) == 1 and not message.isdigit():
            return {"route": "clarify", "confirmed_digest": None, "clarification_notice":
                    "That looks incomplete. Type 'go ahead' to run this plan, or describe the change you want."}
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
