"""Reviewed grammar for complete, unambiguous KPI questions. Unmatched text goes to the interpreter.

Full matches are intentional: dropping an unknown modifier, filter or date would answer a different question.
Relative dates follow the published snapshot, consistently with the interpreter.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from .spec import Catalog, Filter, Measure, QuerySpec, Ranking, TimeSpec, validate

METRICS = {
    "gmv": "gross_sales", "gross sales": "gross_sales", "net sales": "net_sales",
    "revenue": "realized_revenue", "sales": "realized_revenue", "realized revenue": "realized_revenue",
    "orders": "orders", "return rate": "return_rate", "cancellation rate": "cancellation_rate",
    "average order value": "aov", "aov": "aov", "marketing spend": "marketing_spend",
    "conversion rate": "conversion_rate", "sessions": "sessions",
}
DIMENSIONS = {"category": "category", "brand": "brand", "channel": "marketing_channel", "city": "city"}


def independent_history(history: str, cat: Catalog, last_day: str) -> bool:
    """Only previously self-contained questions are proven free of standing instructions.

    Arbitrary prose, corrections and instructions still need interpretation. Analyst output is not an instruction.
    """
    if not history:
        return True
    users = re.findall(r"^User: (.*)$", history, re.M)
    return bool(users) and all(parse(q, cat, last_day) is not None for q in users)


def followup(question: str, previous: QuerySpec | None, cat: Catalog, last_day: str) -> QuerySpec | None:
    """Explicit edits of the last successful plan; preserve its population and metric definitions."""
    if previous is None or previous.columns or previous.ranking or previous.operation:
        return None
    text = question.lower().strip(" ?.!\t\n")
    match = re.fullmatch(r"(?:and |same )?by (category|brand|channel|city)", text)
    if match:
        result = previous.model_copy(update={"dimensions": [DIMENSIONS[match[1]]]})
    else:
        match = re.fullmatch(r"(?:and |the )?same for (yesterday|today|the last (?:[1-9]\d?|seven) days)", text)
        if not match:
            return None
        period = parse(f"orders for {match[1]}", cat, last_day).time
        if previous.time:
            period = period.model_copy(update={"field": previous.time.field, "grain": previous.time.grain})
        result = previous.model_copy(update={"time": period})
    return None if validate(result, cat) else result


def parse(question: str, cat: Catalog, last_day: str) -> QuerySpec | None:
    text = " ".join(question.lower().strip(" ?.!\t\n").split())
    text = re.sub(r"^(?:what (?:is|was|are|were)|show(?: me)?|give me) (?:the )?", "", text)
    # Possessive relative periods, e.g. "yesterday's GMV".
    text = re.sub(r"^(yesterday|today)['’]s (.+)$", r"\2 for \1", text)
    entity = re.fullmatch(r"(.+) where (category|brand|channel|city) is [\"']?([^\"']+)[\"']?", text)
    if entity:
        text = entity[1]
    metrics = "|".join(sorted(map(re.escape, METRICS), key=len, reverse=True))
    pattern = (rf"(?:(daily|weekly|monthly) )?({metrics})"
               r"(?: by (category|brand|channel|city))?"
               r"(?: (?:for |in )?(yesterday|today|the last (?:[1-9]\d?|seven) days|\d{4}-\d{2}-\d{2}))?")
    match = re.fullmatch(pattern, text)
    ranking = None
    if not match:
        ranked = re.fullmatch(rf"top ([1-9]\d?) (brands|categories) by ({metrics})", text)
        if not ranked:
            ranked = re.fullmatch(rf"which (brands|categories) generated the highest ({metrics})", text)
            if not ranked:
                return None
            group, word = ranked.groups()
            count = 10
        else:
            count, group, word = ranked.groups()
        metric = METRICS[word]
        dims = ["brand" if group == "brands" else "category"]
        grain = period = None
        ranking = Ranking(by=metric, limit=int(count))
    else:
        grain, word, dim, period = match.groups()
        metric, dims = METRICS[word], [DIMENSIONS[dim]] if dim else []
    time = None
    if period or grain:
        end = date.fromisoformat(str(last_day)[:10])
        start = None
        if period == "yesterday":
            start = end = end - timedelta(days=1)
        elif period == "today":
            start = end
        elif period and period.startswith("the last"):
            days = period.split()[2]
            start = end - timedelta(days=(7 if days == "seven" else int(days)) - 1)
        elif period:
            try:
                start = end = date.fromisoformat(period)
            except ValueError:
                return None
        else:
            end = None
        time = TimeSpec(start=start, end=end, grain={"daily": "day", "weekly": "week", "monthly": "month"}.get(grain))
    spec = QuerySpec(table=cat.source_table(metric), measures=[Measure(name=metric, metric=metric)],
                     dimensions=dims, time=time, ranking=ranking)
    if entity:
        field = DIMENSIONS[entity[2]]
        try:
            expression, _, entry = cat.resolve(field, spec.table)
            allowed = entry.labels() if entry and entry.expression else cat.values_of(expression)
            plain = lambda s: re.sub(r"[\s_-]+", " ", s.casefold()).strip()  # noqa: E731
            matches = [v for v in allowed or [] if plain(v) == plain(entity[3])]
            if len(matches) != 1:
                return None
            spec = spec.model_copy(update={"filters": [Filter(field=field, op="eq", values=matches)]})
        except (KeyError, ValueError):
            return None
    return None if validate(spec, cat) else spec
