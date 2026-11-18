"""Interpretation: the question becomes a checked QuerySpec (or a template call); anything unsure is asked.

The model may decide WHICH question is answered, never WHICH number is returned (docs/ARCHITECTURE.md):

1. Ambiguity register: phrases this shop knows to have several meanings ("wrong size", "worst reviews") are asked
   first, whatever the model thinks.
2. The interpreter reads the question against the whole catalog (a stable, provider-cached prompt prefix) and returns
   a spec, a template call, questions, a decline, or "unanswerable".
3. Code normalises values to their stored spelling and validates; one repair round with the exact problems; still
   invalid -> the problems become questions.
4. Contrast: a second call proposes materially different readings (definition, filter, split, grouping, ranking).
   Each is compiled and RUN; only if a reading gives different numbers does the user choose. Readings that give the
   same numbers are not worth a question (disagreement-guided clarification, 2026 research).
5. Premise check: a period outside the data is stated, never silently answered as zero.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

import pandas as pd
from pydantic import BaseModel, Field, model_validator

from .agents import ClarifyingQuestion, system_prompt
from .catalog import describe
from .spec import TICKETS, Catalog, QuerySpec, SpecError, compile_spec, validate
from .templates import TemplateError
from .templates import build as build_template
from .templates import describe as describe_templates

MAX_REPAIRS = 1
MAX_ALTERNATIVES = 2
CANNOT_BUILD = "I could not build this exactly"
READINGS = "I can read this question in more than one way"


class TemplateCall(BaseModel):
    """A template and its parameters. Every parameter is a declared field (providers' strict structured output
    rejects free-form objects); unset ones take the template's default."""
    name: Literal["event_lift", "channel_roas", "platform_vs_recorded_orders", "repeat_within_days"]
    days_before: int | None = Field(None, description="event_lift: baseline days before each event")
    by: Literal["channel", "channel_group"] | None = Field(None, description="channel_roas, "
                                                          "platform_vs_recorded_orders: the split")
    days: int | None = Field(None, description="repeat_within_days: window after the first order")

    @model_validator(mode="before")
    @classmethod
    def _flatten(cls, data):
        if isinstance(data, dict) and isinstance(data.get("params"), dict):     # {"name", "params": {...}}
            data = {k: v for k, v in data.items() if k != "params"} | data["params"]
        return data

    @property
    def params(self) -> dict:
        return {k: v for k, v in self.model_dump().items() if k != "name" and v is not None}


class Interpretation(BaseModel):
    decline_reason: str | None = Field(None, description="Only if the request must be declined; decided first")
    decline_category: Literal["personal_data", "raw_dump", "change_data", "instructions", "off_topic"] | None = None
    route: Literal["spec", "template", "clarify", "unanswerable"] = Field(
        "spec", description="spec: one QuerySpec; template: one of the listed analyses; clarify: something material "
        "is open; unanswerable: the data cannot answer it as asked (say what it can answer instead)")
    restated_question: str = Field(description="The question as you will answer it, precise and self-contained")
    spec: QuerySpec | None = Field(None, description="For route spec")
    template: TemplateCall | None = Field(None, description="For route template")
    questions: list[ClarifyingQuestion] = Field(default_factory=list, description="For route clarify")
    assumptions: list[str] = Field(default_factory=list, description="Defaults you applied, in business words")
    unanswerable_reason: str | None = Field(None, description="For route unanswerable: why, and what can be answered")


class Alternatives(BaseModel):
    readings: list[QuerySpec] = Field(default_factory=list, description="Materially different readings, at most 2")
    why: list[str] = Field(default_factory=list, description="For each reading: the word it reads differently")


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
    out.append("\n## Analyses (route template; for multi-step questions a spec cannot express)")
    out.append(describe_templates())
    if terms:
        out.append("\n## Business terms")
        out += [f"- {t}: {m}" for t, m in terms.items()]
    return "\n".join(out)


def ask_model(llm, cat: Catalog, terms: dict[str, str], data: dict, question: str, history: str = "",
              answers: dict[str, str] | None = None, problems: list[str] | None = None,
              background: str = "") -> Interpretation:
    system = system_prompt("interpreter", data_first_day=data["first_day"], data_last_day=data["last_day"]) \
        + "\n\n" + catalog_text(cat, terms)
    parts = []
    if background:
        parts.append(f"## Project instructions and what we already know\n{background}")
    if history:
        parts.append(f"## Earlier in this chat\n{history}")
    parts.append(f"## Question\n{question}")
    if answers:
        parts.append("## The user's answers\n" + "\n".join(f"- {q}: {a}" for q, a in answers.items()))
    if problems:
        parts.append("## Your last spec could not be used; fix exactly these problems\n"
                     + "\n".join(f"- {p}" for p in problems))
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
            if m.where:
                text += " where " + " and ".join(show(f) for f in m.where)
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
    kind: Literal["spec", "template", "clarify", "decline", "unanswerable"]
    spec: QuerySpec | None = None
    template: TemplateCall | None = None
    questions: list[ClarifyingQuestion] = field(default_factory=list)
    required: bool = False                       # a question no default may answer
    restated: str = ""
    assumptions: list[str] = field(default_factory=list)
    reason: str = ""
    readings: list[QuerySpec] = field(default_factory=list)    # options of a READINGS question, in order
    detail: dict = field(default_factory=dict)


def register_questions(question: str, register: dict[str, list[str]], answers: dict[str, str]) -> list:
    """Known two-meaning phrases in the question that the user's answers have not settled."""
    asked = " ".join(answers).casefold()
    out = []
    for phrase, meanings in register.items():
        if re.search(rf"(?<![\w-]){re.escape(phrase)}(?![\w-])", question, re.I) and phrase.casefold() not in asked:
            out.append(ClarifyingQuestion(question=f"What do you mean by '{phrase}'?", options=meanings,
                                          default=meanings[0], why="the two meanings give different numbers"))
    return out


def premise_notes(spec: QuerySpec, data: dict) -> list[str]:
    """Facts the user must know about the question's premise (stated in the read-back and the answer)."""
    notes = []
    first, last = date.fromisoformat(str(data["first_day"])[:10]), date.fromisoformat(str(data["last_day"])[:10])
    if spec.time and (spec.time.start or spec.time.end):
        start, end = spec.time.start or first, spec.time.end or last
        if end < first or start > last:
            notes.append(f"The data covers {first} to {last}; the requested period has no data, so every number is "
                         "zero because nothing was recorded, not because nothing happened.")
        elif start < first or end > last:
            notes.append(f"The data covers {first} to {last}; only that part of the requested period is included.")
    return notes


def _same(a: pd.DataFrame, b: pd.DataFrame) -> bool:
    """Do two results carry the same numbers (ignoring column names, row order and rounding)?"""
    if a.shape != b.shape:
        return False
    def norm(f: pd.DataFrame) -> list:
        rows = [tuple(round(v, 6) if isinstance(v, float) else str(v) for v in r) for r in f.itertuples(index=False)]
        return sorted(rows, key=str)
    return norm(a) == norm(b)


def decide(llm, cat: Catalog, terms: dict[str, str], aliases: dict[str, str], data: dict, question: str,
           history: str = "", answers: dict[str, str] | None = None, register: dict[str, list[str]] | None = None,
           run: Callable[[str], pd.DataFrame] | None = None, contrast: bool = True,
           background: str = "") -> Decision:
    """Read the question and decide: answer (spec / template), ask, decline or say it cannot be answered."""
    answers = answers or {}
    unsettled = register_questions(question, register or {}, answers)
    if unsettled:
        return Decision("clarify", questions=unsettled, required=True, reason="ambiguity register")

    reading = ask_model(llm, cat, terms, data, question, history, answers, background=background)
    detail: dict = {"reading": reading.model_dump(mode="json")}
    for attempt in range(MAX_REPAIRS + 1):
        if reading.decline_reason:
            return Decision("decline", reason=reading.decline_reason, detail=detail)
        if reading.route == "unanswerable":
            return Decision("unanswerable", restated=reading.restated_question, detail=detail,
                            reason=reading.unanswerable_reason or "The data cannot answer this as asked.")
        if reading.route == "clarify" and reading.questions:
            return Decision("clarify", questions=reading.questions, restated=reading.restated_question, detail=detail)
        problems = []
        if reading.route == "template" and reading.template:
            try:
                build_template(reading.template.name, reading.template.params)
                return Decision("template", template=reading.template, restated=reading.restated_question,
                                assumptions=reading.assumptions, detail=detail)
            except TemplateError as error:
                problems = [str(error)]
        elif reading.spec is not None:
            spec = normalise(reading.spec, cat, aliases)
            problems = validate(spec, cat)
            if not problems:
                break
        else:
            problems = ["no spec, template or question was given"]
        if attempt < MAX_REPAIRS:
            reading = ask_model(llm, cat, terms, data, question, history, answers, problems=problems,
                                background=background)
            detail["repair"] = {"problems": problems, "reading": reading.model_dump(mode="json")}
    else:
        rephrase = "Let me rephrase the question"
        questions = [ClarifyingQuestion(question=f"{CANNOT_BUILD}: {p}. What did you mean?", options=[rephrase],
                                        default=rephrase, why="nothing is computed from a guess") for p in problems[:3]]
        return Decision("clarify", questions=questions, required=True, restated=reading.restated_question,
                        detail={**detail, "problems": problems})

    notes = premise_notes(spec, data)
    decision = Decision("spec", spec=spec, restated=reading.restated_question,
                        assumptions=[*reading.assumptions, *notes], detail=detail)
    if not contrast or run is None:
        return decision

    # contrast: materially different readings, compiled and run; ask only when the numbers differ
    alternatives = ask_alternatives(llm, cat, terms, data, question, history, answers, spec)
    detail["alternatives"] = alternatives.model_dump(mode="json")
    try:
        base = run(compile_spec(spec, cat))
    except Exception:                                  # noqa: BLE001 - the main reading is executed later anyway
        return decision
    different = []
    for alt in alternatives.readings[:MAX_ALTERNATIVES]:
        alt = normalise(alt, cat, aliases)
        if validate(alt, cat) or canonical(alt) == canonical(spec):
            continue
        try:
            if not _same(base, run(compile_spec(alt, cat))):
                different.append(alt)
        except (SpecError, Exception):                 # noqa: BLE001 - an alternative that cannot run is no option
            continue
    if not different:
        return decision
    readings = [spec, *different]
    options = reading_options(readings, cat)
    choice = ClarifyingQuestion(question=f"{READINGS}. Which one do you mean?", options=options, default=options[0],
                                why="these readings give different numbers")
    return Decision("clarify", questions=[choice], required=True, restated=reading.restated_question,
                    readings=readings, detail={**detail, "different": len(different)})


def reading_options(readings: list[QuerySpec], cat: Catalog) -> list[str]:
    """One short option per reading: only the read-back lines that differ between the readings."""
    lines = [readback(r, cat).replace("**", "").split("\n") for r in readings]
    shared = set.intersection(*(set(x) for x in lines))
    options = [" · ".join(line for line in x if line not in shared) or " · ".join(x) for x in lines]
    return options if len(set(options)) == len(options) else [" · ".join(x) for x in lines]


def ask_alternatives(llm, cat: Catalog, terms: dict[str, str], data: dict, question: str, history: str,
                     answers: dict[str, str], spec: QuerySpec) -> Alternatives:
    system = system_prompt("contrast", data_first_day=data["first_day"], data_last_day=data["last_day"]) \
        + "\n\n" + catalog_text(cat, terms)
    parts = [f"## Earlier in this chat\n{history}"] if history else []
    parts.append(f"## Question\n{question}")
    if answers:
        parts.append("## The user's answers (settled; do not offer readings against them)\n"
                     + "\n".join(f"- {q}: {a}" for q, a in answers.items()))
    parts.append(f"## The reading chosen so far\n{json.dumps(spec.model_dump(mode='json'))}")
    return llm.ask("contrast", Alternatives, system, "\n\n".join(parts))
