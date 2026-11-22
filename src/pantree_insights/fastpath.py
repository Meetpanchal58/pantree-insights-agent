"""Reviewed grammar for complete, unambiguous KPI questions. Unmatched text goes to the interpreter.

Full matches are intentional: dropping an unknown modifier, filter or date would answer a different question.
Relative dates follow the published snapshot, consistently with the interpreter.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from .spec import Catalog, Measure, QuerySpec, Ranking, TimeSpec, validate

METRICS = {
    "gmv": "gross_sales", "gross sales": "gross_sales", "net sales": "net_sales",
    "revenue": "realized_revenue", "sales": "realized_revenue", "realized revenue": "realized_revenue",
    "orders": "orders", "return rate": "return_rate", "cancellation rate": "cancellation_rate",
    "average order value": "aov", "aov": "aov", "marketing spend": "marketing_spend",
    "conversion rate": "conversion_rate", "sessions": "sessions",
}
DIMENSIONS = {"category": "category", "brand": "brand", "channel": "channel", "city": "city"}


def parse(question: str, cat: Catalog, last_day: str) -> QuerySpec | None:
    text = " ".join(question.lower().strip(" ?.!\t\n").split())
    text = re.sub(r"^(?:what (?:is|was|are|were)|show(?: me)?|give me) (?:the )?", "", text)
    # Possessive relative periods, e.g. "yesterday's GMV".
    text = re.sub(r"^(yesterday|today)['’]s (.+)$", r"\2 for \1", text)
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
    return None if validate(spec, cat) else spec
