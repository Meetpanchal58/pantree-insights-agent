"""v2 interpretation: one model call turns a question into a QuerySpec; code checks it and asks when unsure.

1. The model reads the question against the whole catalog (metrics, cuts, columns, values; a stable prompt prefix
   the provider caches) and returns an Interpretation: a spec, questions for the user, a decline, or "analysis"
   for multi-step questions the spec cannot express.
2. Code normalises values to their stored spelling (aliases, case, underscores) and validates the spec. If it is
   invalid, the model gets one repair round with the exact problems; still invalid -> the problems become
   questions for the user.
3. Agreement: a second, independent reading must produce the same spec. If not, a third reading breaks the tie;
   with no majority the readings become a choice for the user. Disagreement is never resolved by guessing.
4. The read-back the user confirms is rendered from the spec itself, so what is confirmed is what is computed.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from .agents import ClarifyingQuestion, system_prompt
from .catalog import describe
from .spec import TICKETS, Catalog, QuerySpec, validate

MAX_REPAIRS = 1
CANNOT_BUILD = "I could not build this exactly"
READINGS = "I can read this question in more than one way"


class Interpretation(BaseModel):
    decline_reason: str | None = Field(None, description="Only if the request must be declined; decided first")
    decline_category: Literal["personal_data", "raw_dump", "change_data", "instructions", "off_topic"] | None = None
    route: Literal["spec", "analysis", "clarify"] = Field(
        "spec", description="spec: answerable as one QuerySpec; analysis: needs several steps (lift vs a baseline "
        "period, per-customer windows, comparing two tables); clarify: something material is unclear")
    restated_question: str = Field(description="The question as you will answer it, precise and self-contained")
    spec: QuerySpec | None = Field(None, description="For route spec")
    questions: list[ClarifyingQuestion] = Field(default_factory=list, description="For route clarify")
    assumptions: list[str] = Field(default_factory=list, description="Defaults you applied, in business words")


# ----------------------------------------------------------------------------- prompt
def catalog_text(cat: Catalog, terms: dict[str, str]) -> str:
    """Everything a spec may name, as stable text (cached by the provider as the prompt prefix)."""
    out = ["## Tables a spec can be about (table: what one row is; main date)"]
    for name, info in cat.catalog.items():
        subject = cat.subject_of.get(name)
        dates = ""
        if subject:
            s = cat.concepts.subjects[subject]
            others = ", ".join(f"{k}={v}" for k, v in s.time_meanings.items())
            dates = f"; main date {s.time}" + (f"; other dates: {others}" if others else "")
            dates += f"; always {s.where}" if s.where else ""
        out.append(f"- {name}: {info['entity']}{dates}")
    out.append(f"- {TICKETS}: one support ticket (several contacts grouped); main date tickets.opened_at; "
               "columns ticket_id, opened_at, channel, issue_category, resolution_status")
    out.append("\n## Named metrics (use these for these concepts; table = where the metric is measured)")
    for name, m in cat.metrics.metrics.items():
        out.append(f"- {name} ({m.unit}) on {cat.source_table(name)}: {m.label}. {m.description}"
                   + (f" Note: {m.notes}" if m.notes else ""))
    out.append("\n## Cuts (concept names usable as dimensions and filter fields, per table)")
    for subject, s in cat.concepts.subjects.items():
        cuts = [f"{c}={e[subject].sql() if e[subject].expression else e[subject].column}"
                for c, e in cat.concepts.concepts.items() if subject in e]
        out.append(f"- {s.table}: " + "; ".join(cuts))
    out.append("\n## Columns (any table.column below may also be used; values are the exact stored spellings)")
    out.append(describe(list(cat.catalog), catalog=cat.catalog))
    if terms:
        out.append("\n## Business terms")
        out += [f"- {t}: {m}" for t, m in terms.items()]
    return "\n".join(out)


def ask_model(llm, cat: Catalog, terms: dict[str, str], data: dict, question: str, history: str = "",
              answers: dict[str, str] | None = None, problems: list[str] | None = None,
              independent: bool = False) -> Interpretation:
    system = system_prompt("interpreter", data_first_day=data["first_day"], data_last_day=data["last_day"]) \
        + "\n\n" + catalog_text(cat, terms)
    parts = []
    if history:
        parts.append(f"## Earlier in this chat\n{history}")
    parts.append(f"## Question\n{question}")
    if answers:
        parts.append("## The user's answers\n" + "\n".join(f"- {q}: {a}" for q, a in answers.items()))
    if problems:
        parts.append("## Your last spec could not be used; fix exactly these problems\n"
                     + "\n".join(f"- {p}" for p in problems))
    if independent:
        parts.append("Read the question from scratch and build the spec independently.")
    return llm.ask("interpreter", Interpretation, system, "\n\n".join(parts))


# ----------------------------------------------------------------------------- values in their stored spelling
def _plain(text: str) -> str:
    return " ".join(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w
                    for w in re.sub(r"[-_]", " ", str(text).casefold()).split())


def normalise(spec: QuerySpec, cat: Catalog, aliases: dict[str, str]) -> QuerySpec:
    """Map filter values to the stored spelling when that is unambiguous ('organic search' -> organic_search,
    'cash on delivery' -> COD). Anything not matched is left for validation to report."""
    def fix(filters):
        out = []
        for f in filters:
            try:
                expression, _, entry = cat.resolve(f.field, spec.table)
            except Exception:                          # noqa: BLE001 - validation reports it
                out.append(f)
                continue
            allowed = entry.labels() if entry is not None and entry.expression else cat.values_of(expression)
            if not allowed or f.op not in ("eq", "in", "not_in"):
                out.append(f)
                continue
            values = []
            for v in f.values:
                if isinstance(v, bool) or str(v) in allowed:
                    values.append(v)
                    continue
                alias = aliases.get(str(v).casefold())
                matches = ([a for a in allowed if a == alias]
                           or [a for a in allowed if a.casefold() == str(v).casefold()]
                           or [a for a in allowed if _plain(a) == _plain(v)])
                values.append(matches[0] if len(matches) == 1 else v)
            out.append(f.model_copy(update={"values": values}))
        return out
    measures = [m.model_copy(update={"where": fix(m.where)}) for m in spec.measures]
    subject = cat.subject_of.get(spec.table)
    always = cat.concepts.subjects[subject].where if subject else None

    def redundant(f) -> bool:                          # the table's own rule restated (line_type = 'Sale')
        if not always or f.op not in ("eq", "in") or len(f.values) != 1:
            return False
        try:
            expression = cat.resolve(f.field, spec.table)[0]
        except Exception:                              # noqa: BLE001 - validation reports it
            return False
        return f"{expression} = '{f.values[0]}'" == always
    filters = [f for f in fix(spec.filters) if not redundant(f)]
    return spec.model_copy(update={"filters": filters, "measures": measures})


# ----------------------------------------------------------------------------- agreement
def canonical(spec: QuerySpec) -> str:
    """What the spec computes, independent of names and order: two readings agree if this is equal."""
    def filt(f):
        return (f.field, f.op, sorted(map(str, f.values)), f.other)

    def measure(m):
        return (m.metric, m.agg, m.field, sorted(filt(f) for f in m.where), tuple(m.ratio or ()))
    by_name = {m.name: measure(m) for m in spec.measures}
    ratio_fixed = [(m.metric, m.agg, m.field, sorted(filt(f) for f in m.where),
                    tuple(json.dumps(by_name.get(r), default=str) for r in (m.ratio or ())))
                   for m in spec.measures]
    return json.dumps({
        "table": spec.table, "measures": sorted(json.dumps(x, default=str) for x in ratio_fixed),
        "dimensions": sorted(spec.dimensions),
        "filters": sorted(json.dumps(filt(f), default=str) for f in spec.filters),
        "time": spec.time.model_dump(mode="json") if spec.time else None,
        "ranking": [json.dumps(by_name.get(spec.ranking.by), default=str), spec.ranking.order, spec.ranking.limit]
        if spec.ranking else None,
        "shares": sorted(json.dumps(by_name.get(s), default=str) for s in spec.shares),
        "columns": sorted(spec.columns), "limit": spec.limit,
    }, sort_keys=True, default=str)


# ----------------------------------------------------------------------------- read-back from the spec
def readback(spec: QuerySpec, cat: Catalog, assumptions: list[str] | None = None) -> str:
    def label(field: str) -> str:
        if "." in field:
            return field.split(".", 1)[1].replace("_", " ")
        return cat.concepts.label(field)

    def show(f) -> str:
        if f.op == "is_set":
            return f"{label(f.field)} is recorded"
        if f.op == "is_not_set":
            return f"{label(f.field)} is not recorded"
        if f.op.endswith("_field"):
            return f"{label(f.field)} {f.op.split('_')[0].replace('lte', 'at most').replace('gte', 'at least')} " \
                   f"{label(f.other or '')}"
        if f.op in ("lt", "lte", "gt", "gte"):
            return f"{label(f.field)} {dict(lt='<', lte='≤', gt='>', gte='≥')[f.op]} {f.values[0]}"
        words = " or ".join(str(v) for v in f.values)
        return f"{label(f.field)} {'is not' if f.op == 'not_in' else 'is'} {words}"

    entity = cat.catalog.get(spec.table, {}).get("entity", "ticket" if spec.table == TICKETS else spec.table)
    lines = []
    if spec.columns:
        lines.append(f"**List:** {', '.join(label(c) for c in spec.columns)} of each {entity} "
                     f"(at most {spec.limit or 100})")
    for m in spec.measures:
        if m.metric:
            metric = cat.metrics.metrics[m.metric]
            text = f"{metric.label}: {metric.description}"
        elif m.ratio:
            text = f"{m.name.replace('_', ' ')} = {m.ratio[0].replace('_', ' ')} / {m.ratio[1].replace('_', ' ')}"
        elif m.agg == "share":
            text = f"{m.name.replace('_', ' ')}: share of {entity} rows where " + " and ".join(show(f) for f in m.where)
        else:
            what = "number of rows" if m.agg == "count" else f"{m.agg.replace('_', ' ')} of {label(m.field or '')}"
            cond = (" where " + " and ".join(show(f) for f in m.where)) if m.where else ""
            text = f"{m.name.replace('_', ' ')}: {what}{cond} (one row = {entity})"
        lines.append(f"**Measure:** {text}")
    splits = [label(d) for d in spec.dimensions] + ([spec.time.grain] if spec.time and spec.time.grain else [])
    if splits:
        lines.append(f"**Split by:** {', '.join(splits)}")
    if spec.time and (spec.time.start or spec.time.end):
        when = f"{spec.time.start or 'the start of the data'} to {spec.time.end or 'the latest data'}"
        lines.append(f"**Period:** {when}" + (f", by {label(spec.time.field)}" if spec.time.field else ""))
    else:
        lines.append("**Period:** all available data")
    lines.append("**Filters:** " + ("; ".join(show(f) for f in spec.filters) if spec.filters else "none"))
    if spec.ranking:
        lines.append(f"**Ranking:** {'top' if spec.ranking.order == 'desc' else 'bottom'} {spec.ranking.limit} "
                     f"by {spec.ranking.by.replace('_', ' ')}")
    if spec.shares:
        lines.append("**Also shown:** share of the total for " + ", ".join(s.replace("_", " ") for s in spec.shares))
    if assumptions:
        lines.append("**Assumed:** " + "; ".join(assumptions))
    return "\n".join(lines)


# ----------------------------------------------------------------------------- the decision
@dataclass
class Decision:
    kind: Literal["spec", "analysis", "clarify", "decline"]
    spec: QuerySpec | None = None
    questions: list[ClarifyingQuestion] = field(default_factory=list)
    restated: str = ""
    assumptions: list[str] = field(default_factory=list)
    reason: str = ""
    readings: int = 0
    detail: dict = field(default_factory=dict)


def decide(llm, cat: Catalog, terms: dict[str, str], aliases: dict[str, str], data: dict, question: str,
           history: str = "", answers: dict[str, str] | None = None, agreement: bool = True) -> Decision:
    """Read the question (and, for agreement, re-read it independently) and decide what happens next."""
    def one(independent: bool) -> tuple[Interpretation, QuerySpec | None, list[str]]:
        reading = ask_model(llm, cat, terms, data, question, history, answers, independent=independent)
        problems: list[str] = []
        for attempt in range(MAX_REPAIRS + 1):
            if reading.decline_reason or reading.route != "spec" or reading.spec is None:
                return reading, None, []
            spec = normalise(reading.spec, cat, aliases)
            problems = validate(spec, cat)
            if not problems:
                return reading, spec, []
            if attempt < MAX_REPAIRS:
                reading = ask_model(llm, cat, terms, data, question, history, answers, problems=problems,
                                    independent=independent)
        return reading, None, problems

    first, spec, problems = one(independent=False)
    detail = {"first": first.model_dump(mode="json")}
    if first.decline_reason:
        if first.decline_category == "raw_dump":           # lists of up to 100 rows are analysis: second opinion
            return Decision("analysis", restated=first.restated_question, reason="possible raw dump", readings=1)
        return Decision("decline", reason=first.decline_reason, readings=1)
    if first.route == "analysis":
        return Decision("analysis", restated=first.restated_question, reason="multi-step analysis", readings=1)
    if first.route == "clarify" and first.questions:
        return Decision("clarify", questions=first.questions, restated=first.restated_question, readings=1)
    if spec is None:
        rephrase = "Let me rephrase the question"
        questions = [ClarifyingQuestion(question=f"{CANNOT_BUILD}: {p}. What did you mean?", options=[rephrase],
                                        default=rephrase, why="nothing is computed from a guess")
                     for p in problems[:3]] or first.questions
        return Decision("clarify", questions=questions, restated=first.restated_question, readings=1,
                        detail={**detail, "problems": problems})
    if not agreement:
        return Decision("spec", spec=spec, restated=first.restated_question, assumptions=first.assumptions,
                        readings=1, detail=detail)
    second, spec2, _ = one(independent=True)
    detail["second"] = second.model_dump(mode="json")
    if spec2 is not None and canonical(spec2) == canonical(spec):
        return Decision("spec", spec=spec, restated=first.restated_question, assumptions=first.assumptions,
                        readings=2, detail=detail)
    third, spec3, _ = one(independent=True)
    detail["third"] = third.model_dump(mode="json")
    keys = [canonical(s) if s is not None else None for s in (spec, spec2, spec3)]
    for candidate, key in zip((spec, spec2, spec3), keys, strict=True):
        if key is not None and keys.count(key) >= 2:
            chosen = (first, second, third)[keys.index(key)]
            return Decision("spec", spec=candidate, restated=chosen.restated_question,
                            assumptions=chosen.assumptions, readings=3, detail=detail)
    readings = [s for s in (spec, spec2, spec3) if s is not None]
    options = list(dict.fromkeys(readback(s, cat).replace("\n", " · ") for s in readings))[:3]
    question_text = f"{READINGS}. Which one do you mean?"
    choice = ClarifyingQuestion(question=question_text, options=options + ["None of these"], default=options[0],
                                why="different readings give different numbers")
    return Decision("clarify", restated=first.restated_question, readings=3, detail={**detail, "readings": options},
                    questions=[choice])
