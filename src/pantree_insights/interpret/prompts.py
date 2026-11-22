"""Interpret: prompts."""
from __future__ import annotations

import json

from ..agents import system_prompt
from ..catalog import describe
from ..spec import TICKETS, Catalog, QuerySpec
from ..templates import describe as describe_templates
from .models import Alternatives, Interpretation


def catalog_text(cat: Catalog, terms: dict[str, str]) -> str:
    """Everything a spec may name, as stable text (cached by the provider as the prompt prefix)."""
    key = tuple(sorted(terms.items()))
    if getattr(cat, "_prompt_key", None) == key:
        return cat._prompt_text
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
        out.append(f"- {name} ({m.unit}; time {m.time_aggregation}) on {cat.source_table(name)}: "
                   f"{m.label}. {m.description}"
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
    cat._prompt_key, cat._prompt_text = key, "\n".join(out)
    return cat._prompt_text


def ask_model(llm, cat: Catalog, terms: dict[str, str], data: dict, question: str, history: str = "",
              answers: dict[str, str] | None = None, problems: list[str] | None = None,
              background: str = "", prior_reading: Interpretation | None = None) -> Interpretation:
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
    if prior_reading is not None:
        parts.append("## Your previous proposed structure to repair\n" + prior_reading.model_dump_json())
    if problems:
        parts.append("## Your last spec could not be used; fix exactly these problems\n"
                     + "\n".join(f"- {p}" for p in problems))
    return llm.ask("interpreter", Interpretation, system, "\n\n".join(parts))


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
