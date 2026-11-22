"""Bind follow-up populations to complete verified results, never to a prose summary or model guesses."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from .spec import Catalog, Filter, QuerySpec, SpecError


class ResultReference(BaseModel):
    run_id: str | None = Field(None, description="Retrieved historical run ID, or null for the latest result")
    query_index: int = Field(ge=0, le=7, description="Zero-based query in the referenced verified result")
    field: str = Field(description="One available population field; values are resolved by the server")


def refers_to_results(question: str) -> bool:
    """A reference to groups from an earlier result ("these brands", "the same sellers"). Pronouns ("their average",
    "reach them") and relative clauses ("those who returned") refer within the question, not to a result."""
    return bool(
        re.search(
            r"\b(?:these|those)\b(?! (?:who|that|which|whose|with|where|of the)\b)|"
            r"\b(?:same|above|previous|earlier) "
            r"(?:results?|brands?|products?|groups?|categories|sellers?|customers?)\b",
            question,
            re.I,
        )
    )


def populations(plan: list[dict], results: list[dict], cat: Catalog, snapshot: str | None) -> list[dict]:
    out = []
    for index, (item, result) in enumerate(zip(plan, results, strict=False)):
        if not item.get("spec"):
            continue
        spec = QuerySpec.model_validate(item["spec"])
        data = result.get("result", result)
        rows, columns = data.get("data", []), data.get("columns", [])
        complete = (
            not data.get("truncated")
            and not data.get("excerpted")
            and len(rows) <= 500
            and data.get("row_count", len(rows)) == len(rows)
        )
        for field in spec.dimensions:
            expression, name, _ = cat.resolve(field, spec.table)
            identity = cat.labels.get(expression)
            output = identity or name
            target = f"{expression.split('.')[0]}.{identity}" if identity else field
            if output not in columns:
                output = f"{expression.split('.')[0]}_{output}"
            if output not in columns:
                continue
            resolved = cat.resolve(target, spec.table)[0]
            if any(p["query_index"] == index and p["expression"] == resolved for p in out):
                continue
            values = list(dict.fromkeys(row[columns.index(output)] for row in rows)) if complete else []
            # A null group cannot be expressed as an IN filter without changing its population.
            usable = complete and bool(values) and None not in values
            out.append(
                {
                    "query_index": index,
                    "field": field,
                    "filter_field": target,
                    "expression": resolved,
                    "values": values if usable else [],
                    "complete": usable,
                    "snapshot": snapshot,
                    "table": spec.table,
                    "time": spec.time.model_dump(mode="json") if spec.time else None,
                    "filters": [f.model_dump(mode="json") for f in spec.filters],
                    "measures": [m.model_dump(mode="json") for m in spec.measures],
                    "ranking": spec.ranking.model_dump(mode="json") if spec.ranking else None,
                }
            )
    return out


def saved_populations(context: dict, cat: Catalog) -> list[dict]:
    if "populations" in context:
        return [*context["populations"], *context.get("historical_populations", [])]
    evidence = (context.get("evidence") or [{}])[-1]
    return [*populations(evidence.get("plan", []), evidence.get("results", []), cat, evidence.get("snapshot")),
            *context.get("historical_populations", [])]


def choose_population(question: str, choices: list[dict], reference: ResultReference | None = None) -> dict:
    matches = choices
    if reference:
        matches = [p for p in choices if p["query_index"] == reference.query_index and p["field"] == reference.field
                   and p.get("run_id") == reference.run_id]
    else:
        mentioned = []
        for p in choices:
            if p.get("run_id"):
                continue
            word = p["field"].split(".")[-1].removesuffix("_name").removesuffix("_id").replace("_", " ")
            forms = (word, word + "s", word[:-1] + "ies" if word.endswith("y") else word)
            if any(re.search(rf"\b{re.escape(w)}\b", question, re.I) for w in forms):
                mentioned.append(p)
        matches = mentioned or [p for p in choices if not p.get("run_id")]
    if len(matches) != 1:
        raise SpecError("Which earlier result and group should this follow-up use? Name the query and group.")
    return matches[0]


def bind_population(spec: QuerySpec, population: dict, cat: Catalog, snapshot: str | None, question: str) -> QuerySpec:
    if not population["complete"] or not snapshot or population.get("snapshot") != snapshot:
        raise SpecError("The earlier group is incomplete or from a different snapshot. Re-run its analysis first.")
    field = population["filter_field"]
    if cat.resolve(field, spec.table)[0] != population["expression"]:
        raise SpecError(
            "That group has a different meaning on the requested table; choose a shared business dimension."
        )
    # Replace a guessed IN list for the exact referenced dimension with its recorded values.
    filters = [
        f
        for f in spec.filters
        if cat.resolve(f.field, spec.table)[0] != population["expression"]
        or f.op != "in"
        and f.op != "eq"
        or any(re.search(rf"(?<!\w){re.escape(str(v))}(?!\w)", question, re.I) for v in f.values)
    ]
    filters.append(Filter(field=field, op="in", values=population["values"]))
    if spec.table == population["table"]:
        for raw in population["filters"]:
            old = Filter.model_validate(raw)
            if cat.resolve(old.field, spec.table)[0] == population["expression"]:
                continue
            if not any(cat.resolve(f.field, spec.table)[0] == cat.resolve(old.field, spec.table)[0] for f in filters):
                filters.append(old)
    # Absent a new period, retain the reporting period on the same fact. Cross-fact date meanings stay explicit.
    period = spec.time
    explicit_period = re.search(
        r"\b20\d{2}\b|\b(today|yesterday|current|last|this|all time|all available|all data|entire dataset)\b|"
        r"\b(?:previous|next) (?:day|week|month|quarter|year)\b|\bq[1-4]\b|"
        r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\b",
        question,
        re.I,
    )
    if not explicit_period and spec.table == population["table"] and population.get("time"):
        from .spec import TimeSpec

        inherited = {**population["time"], "grain": period.grain if period else None}
        period = TimeSpec.model_validate(inherited)
    return spec.model_copy(update={"filters": filters, "time": period})
