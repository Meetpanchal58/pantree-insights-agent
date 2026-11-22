"""Interpret: decide."""
from __future__ import annotations

import json
import re
from datetime import date

from ..agents import ClarifyingQuestion
from ..context import bind_population, choose_population, refers_to_results, saved_populations
from ..fastpath import followup, independent_history
from ..fastpath import parse as fast_spec
from ..spec import Catalog, QuerySpec, validate
from ..templates import TemplateError
from ..templates import build as build_template
from .checks import coverage_problems, intent_problems, template_intent_problems
from .models import MAX_ALTERNATIVES, MAX_REPAIRS, READINGS, Decision
from .prompts import ask_alternatives, ask_model
from .readings import canonical, normalise, premise_notes, reading_options, register_questions


def decide(llm, cat: Catalog, terms: dict[str, str], aliases: dict[str, str], data: dict, question: str,
           history: str = "", answers: dict[str, str] | None = None, register: dict[str, list[str]] | None = None,
           contrast: bool = True,
           background: str = "", previous: QuerySpec | None = None,
           investigation: dict | None = None, snapshot: str | None = None) -> Decision:
    """Read the question and decide: answer (spec / template), ask, decline or say it cannot be answered."""
    answers = answers or {}
    effective_question = question
    for prompt, answer in answers.items():
        if re.search(r"\b(measure|metric)\b", prompt, re.I):
            from ..fastpath import METRICS
            for phrase in sorted(METRICS, key=len, reverse=True):
                effective_question = re.sub(rf"\b{re.escape(phrase)}\b", "", effective_question, flags=re.I)
        if re.search(r"\bby (category|brand|city|channel)\b", answer, re.I):
            effective_question = re.sub(r"\bby (category|brand|city|channel)\b", "", effective_question, flags=re.I)
    effective_question += " " + " ".join(answers.values())
    referential = refers_to_results(question) or bool(re.search(
        r"\b(?:earlier|previous|old|original) (?:analysis|investigation)\b|\brun [a-f0-9]{12,32}\b", question, re.I))
    available = saved_populations(investigation or {}, cat)
    register = dict(register or {})
    from ..fastpath import METRICS
    explicit_metric = any(re.search(rf"\b{re.escape(phrase)}\b", effective_question, re.I) for phrase in METRICS)
    inherited_metric = None
    if (referential and previous and re.search(r"\b(performing|performance)\b", question, re.I)
            and not re.search(r"\b(earlier|previous|old|original|run)\b", question, re.I)):
        ranked = next((m.metric for m in previous.measures if previous.ranking and m.name == previous.ranking.by), None)
        inherited_metric = ranked or (previous.measures[0].metric if len(previous.measures) == 1 else None)
    if explicit_metric or inherited_metric:
        register = {k: v for k, v in register.items() if k not in ('performing', 'performance')}
    if inherited_metric and not explicit_metric:
        effective_question += " " + inherited_metric.replace("_", " ")
        background += f"\nThe established performance measure is {inherited_metric}; retain it for this follow-up."
    if referential:
        if not available:
            return Decision('clarify', required=True, questions=[ClarifyingQuestion(
                question='Which earlier groups should I use? Please run their analysis first or name the groups.',
                options=[], default='', why='A result reference needs complete verified evidence')])
        choices = [{k: p.get(k) for k in ('run_id', 'query_index', 'field', 'complete')} for p in available]
        background += "\nAvailable result populations (server binds the exact members): " + json.dumps(choices)
    unsettled = register_questions(question, register, answers)
    if unsettled:
        return Decision("clarify", questions=unsettled, required=True, reason="ambiguity register")

    if not answers and background in ("", "(nothing remembered yet)") and independent_history(
            history, cat, data["last_day"]):
        direct = fast_spec(question, cat, data["last_day"]) or followup(question, previous, cat, data["last_day"])
        if direct is not None:
            notes = premise_notes(direct, data)
            if re.search(r"\b(today|yesterday|last)\b", question, re.I):
                notes.append(f"Relative dates are measured from the snapshot's last day, {data['last_day']}.")
            return Decision("spec", spec=direct, restated=question, assumptions=notes,
                            detail={"fast_path": True})

    if previous is not None:
        background += "\nPrevious confirmed plan (retain unchanged scope for follow-ups): " + previous.model_dump_json()
    reading = ask_model(llm, cat, terms, data, question, history, answers, background=background)
    detail: dict = {"reading": reading.model_dump(mode="json"), "relationship": reading.relationship,
                    "adaptive": reading.adaptive}
    for attempt in range(MAX_REPAIRS + 1):
        detail["adaptive"] = reading.adaptive
        if reading.result_scope is not None:
            referential = True
        if reading.decline_reason:
            return Decision("decline", reason=reading.decline_reason, detail=detail)
        if reading.route == "unanswerable":
            return Decision("unanswerable", restated=reading.restated_question, detail=detail,
                            reason=reading.unanswerable_reason or "The data cannot answer this as asked.")
        if reading.route == "clarify" and reading.questions:
            remaining = [q for q in reading.questions if q.question not in answers and not (
                referential and inherited_metric and re.search(
                    r"\b(?:which|what) (?:metric|measure)\b|what do you mean by ['\"]?perform",
                    q.question, re.I))]
            if remaining:
                return Decision("clarify", questions=remaining, restated=reading.restated_question, detail=detail)
            if attempt < MAX_REPAIRS:
                reading = ask_model(llm, cat, terms, data, question, history, answers,
                    problems=["These questions are already settled by the user's answers or established measure. "
                              "Use that context to build the requested plan; do not ask them again."],
                    background=background, prior_reading=reading)
                continue
            return Decision("failed", reason="I could not apply the established scope to this follow-up. "
                            "No analysis was executed; your earlier results are saved.", detail=detail)
        problems = []
        if referential and reading.route in ('spec', 'investigation', 'template'):
            try:
                population = choose_population(question, available, reading.result_scope)
                plans = [step.spec for step in reading.steps] if reading.route == 'investigation' else [reading.spec]
                if any(plan is None for plan in plans):
                    raise ValueError('This result population needs a governed query rather than an unscoped template.')
                bound = [bind_population(plan, population, cat, snapshot, effective_question) for plan in plans]
                if reading.route == 'investigation':
                    for step, plan in zip(reading.steps, bound, strict=True):
                        step.spec = plan
                else:
                    reading.spec = bound[0]
                reading.relationship = 'drilldown' if reading.relationship == 'new' else reading.relationship
                detail['relationship'] = reading.relationship
            except ValueError as error:
                return Decision('clarify', required=True, questions=[ClarifyingQuestion(
                    question=str(error), options=[], default='', why='Preserve the exact referenced population')])
        if reading.route == "investigation" and reading.steps:
            metrics = {m.metric for step in reading.steps if step.spec for m in step.spec.measures}
            for step in reading.steps:
                if step.spec:
                    step.spec = normalise(step.spec, cat, aliases)
                    problems += validate(step.spec, cat)
                    intent = intent_problems(effective_question, step.spec, cat, data["last_day"])
                    # Metrics may be answered by another approved step, but populations cannot silently disappear.
                    problems += [p for p in intent if not (
                        p.startswith("the question explicitly requests ")
                        and any(f"requests {m};" in p for m in metrics)
                        or p.startswith("the question asks for a split by ") and any(
                            p not in intent_problems(effective_question, other.spec, cat, data["last_day"])
                            for other in reading.steps if other.spec))]
                else:
                    try:
                        build_template(step.template.name, step.template.params)
                        problems += template_intent_problems(effective_question, step.template, cat, data)
                    except TemplateError as error:
                        problems.append(str(error))
                problems += coverage_problems(step.spec, step.template, data)
            if not problems:
                return Decision("investigation", steps=reading.steps, restated=reading.restated_question,
                                assumptions=reading.assumptions, detail=detail)
        elif reading.route == "template" and reading.template:
            try:
                build_template(reading.template.name, reading.template.params)
                scope_errors = template_intent_problems(effective_question, reading.template, cat, data)
                if scope_errors:
                    raise TemplateError("; ".join(scope_errors))
                # Reviewed SQL is not evidence that its unrestricted scope matches this request.
                dated = reading.template.name in ("cohort_retention", "session_funnel", "customer_segments")
                if dated:
                    requested = set(re.findall(r"\b\d{4}-\d{2}-\d{2}\b", question))
                    supplied = {str(reading.template.start), str(reading.template.end), str(reading.template.as_of)}
                    if not requested <= supplied:
                        raise TemplateError("the analysis date bounds omit an explicit requested date")
                if dated and reading.template.as_of and reading.template.as_of > date.fromisoformat(data["last_day"]):
                    raise TemplateError("as_of exceeds the observed dataset; future retention is unknown")
                if not dated and re.search(r"\b(?:20\d{2}|january|february|march|april|may|june|july|august|september|"
                             r"october|november|december|yesterday|today|last (?:week|month|year|\d+ days))\b",
                             question, re.I):
                    raise TemplateError("this template has no date filter; use a dated spec or clarify the scope")
                return Decision("template", template=reading.template, restated=reading.restated_question,
                                assumptions=reading.assumptions, detail=detail)
            except TemplateError as error:
                problems = [str(error)]
        elif reading.spec is not None:
            spec = normalise(reading.spec, cat, aliases)
            problems = validate(spec, cat)
            if not problems:
                problems = intent_problems(effective_question.strip(), spec, cat, data["last_day"])
            if not problems:
                break
        else:
            problems = ["no spec, template or question was given"]
        if attempt < MAX_REPAIRS:
            reading = ask_model(llm, cat, terms, data, question, history, answers, problems=problems,
                                background=background, prior_reading=reading)
            detail["repair"] = {"problems": problems, "reading": reading.model_dump(mode="json")}
    else:
        # An explicitly requested, absent business value is a user-resolvable ambiguity.
        # Invented fields, aliases or a wrong metric are planner failures, not wording problems.
        missing = [p for p in problems if " has no value " in p and any(
            re.search(rf"(?<!\w){re.escape(v)}(?!\w)", effective_question, re.I)
            for v in re.findall(r"'([^']+)'", p.split(";", 1)[0]))]
        if missing:
            return Decision("clarify", required=True, questions=[ClarifyingQuestion(
                question="; ".join(missing) + ". Which recorded value should I use?",
                options=[], default="", why="The requested group is absent from this snapshot")], detail=detail)
        return Decision("failed", reason="I couldn't construct a valid analysis for this request. "
                        "This is a planning limitation, not a problem with your wording. "
                        "No analytical query was executed; your earlier results are still available.",
                        restated=reading.restated_question, detail={**detail, "problems": problems})

    notes = premise_notes(spec, data)
    decision = Decision("spec", spec=spec, restated=reading.restated_question,
                        assumptions=[*reading.assumptions, *notes], detail=detail)
    direct = fast_spec(question, cat, data["last_day"])
    explicit_low_risk = (not spec.operation and not spec.shares and not spec.ranking and len(spec.measures) == 1
                         and not reading.assumptions and not background and not history and not answers
                         and not spec.filters and not spec.measures[0].where
                         and spec.measures[0].metric is not None
                         and spec.measures[0].metric.replace("_", " ") in question.casefold()
                         and all(d.replace("_", " ") in question.casefold() for d in spec.dimensions))
    bound_drilldown = (referential and inherited_metric and not reading.assumptions
                       and {m.metric for m in spec.measures} == {inherited_metric}
                       and not spec.operation and not spec.shares
                       and not re.search(r"\b(why|cause|drivers?|explain)\b", question, re.I))
    if not contrast or explicit_low_risk or bound_drilldown or (not answers
                        and background in ("", "(nothing remembered yet)")
                        and direct is not None and canonical(direct) == canonical(spec)):
        detail["contrast_skipped"] = "disabled, explicit scope, bound result drilldown, or reviewed intent"
        return decision

    # Semantic differences matter even when a snapshot happens to produce equal values.
    # No speculative database executions: truncated or coincidentally equal results prove nothing.
    alternatives = ask_alternatives(llm, cat, terms, data, question, history, answers, spec)
    detail["alternatives"] = alternatives.model_dump(mode="json")
    different = []
    for alt in alternatives.readings[:MAX_ALTERNATIVES]:
        if referential:
            try:
                alt = bind_population(alt, population, cat, snapshot, effective_question)
            except ValueError:
                continue
        alt = normalise(alt, cat, aliases)
        if (validate(alt, cat) or intent_problems(effective_question, alt, cat, data["last_day"])
                or canonical(alt) == canonical(spec)):
            continue
        different.append(alt)
    if not different:
        return decision
    readings = [spec, *different]
    options = reading_options(readings, cat)
    choice = ClarifyingQuestion(question=f"{READINGS}. Which one do you mean?", options=options, default=options[0],
                                why="these readings use different business definitions or populations")
    return Decision("clarify", questions=[choice], required=True, restated=reading.restated_question,
                    readings=readings, detail={**detail, "different": len(different)})
