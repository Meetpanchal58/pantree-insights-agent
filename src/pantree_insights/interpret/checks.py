"""Interpret: checks."""
from __future__ import annotations

import re
from datetime import date, timedelta

from ..fastpath import parse as fast_spec
from ..spec import Catalog, Measure, QuerySpec, TimeSpec, validate
from .models import TemplateCall


def coverage_problems(spec: QuerySpec | None, call: TemplateCall | None, data: dict) -> list[str]:
    """Coverage gaps that would make a comparison misleading: a comparison, rolling or cohort window that is only
    partly observed. A plain requested period that runs past the data ("in 2026" with data to 22 November) is not a
    problem: it is answered for the observed days and the read-back says so (readings.premise_notes)."""
    first, last = (date.fromisoformat(str(data[k])[:10]) for k in ("first_day", "last_day"))
    periods = []
    if spec and spec.time:
        current = (spec.time.start or first, spec.time.end or last)
        if spec.operation:
            op = spec.operation
            if op.kind == "period_compare" and op.previous_start and op.previous_end:
                periods.append(("current period", *current))         # a partial side would compare unequal periods
                periods.append(("comparison period", op.previous_start, op.previous_end))
                if current[1] - current[0] != op.previous_end - op.previous_start:
                    return ["comparison totals require equal-duration periods; choose comparable dates"]
            if op.kind == "rolling_sum":
                periods.append(("rolling lookback", current[0] - timedelta(days=op.days - 1), current[1]))
    if call and call.as_of:
        periods.append(("cohort observation window", call.start or first, call.as_of))
    return [f"{label} ({start} to {end}) is not fully observed; the dataset covers {first} to {last}"
            for label, start, end in periods if start < first or end > last]


def template_intent_problems(question: str, call: TemplateCall, cat: Catalog, data: dict) -> list[str]:
    table = "clickstream" if call.name == "session_funnel" else "sales"
    by = "marketing_channel" if call.by == "channel" else "channel_group" \
        if call.by == "channel_group" else call.by
    proxy = QuerySpec(table=table, measures=[Measure(name="scope", metric="sessions" if table == "clickstream"
                                                   else "realized_revenue")],
                      dimensions=[by] if by else [], filters=call.filters or [],
                      time=TimeSpec(start=call.start, end=call.end, grain=call.grain))
    supported = {
        "event_lift": {"net_sales", "realized_revenue"},
        "channel_roas": {"last_click_revenue", "marketing_spend", "roas", "realized_revenue"},
        "platform_vs_recorded_orders": {"orders", "platform_orders"},
        "repeat_within_days": {"orders"}, "cohort_retention": {"orders"},
        "session_funnel": {"sessions", "conversion_rate"},
        "customer_segments": {"orders", "realized_revenue"},
    }[call.name]
    problems = [p for p in intent_problems(question, proxy, cat, data["last_day"])
                if not any(p.startswith(f"the question explicitly requests {metric};") for metric in supported)]
    if call.name == "event_lift":
        # "daily net sales" is the template's per-day comparison, not a request for a day-by-day series.
        problems = [p for p in problems if p != "the question requests day aggregation"]
    # Observation dates are a third endpoint for cohort templates, not a missing reporting bound.
    dates = set(re.findall(r"\b\d{4}-\d{2}-\d{2}\b", question))
    if dates <= {str(call.start), str(call.end), str(call.as_of)}:
        problems = [p for p in problems if p != "the interpreted date bounds omit an explicit requested date"]
    if call.filters:
        problems += validate(proxy, cat)
    return problems + coverage_problems(None, call, data)


# Qualified phrases that name a different measure than their last word ("planned revenue" is not realized revenue).
# Regex keys; a None value consumes the phrase without requiring a governed metric.
QUALIFIED = {
    r"last-click (?:credited )?revenue": "last_click_revenue",
    r"(?:credited|attributed) revenue": "last_click_revenue",
    r"platform-reported roas": "platform_roas",
    r"(?:planned|target|targeted) (?:revenue|sales|orders|spend)": None,
    r"(?:lost|lose|losing) (?:the most |more |most )?(?:sales|revenue|orders)": None,
}


POPULATION_ORDERS = re.compile(
    r"\b(?:placed|place|placing|made|make|making|with|who ordered)\s+(?:an?\s+|any\s+|at least one\s+)?orders?\b", re.I)
ASKED_ORDERS = re.compile(r"\bhow many orders\b|\borders and\b|\band orders\b", re.I)


def intent_problems(question: str, spec: QuerySpec, cat: Catalog, last_day: str | None = None) -> list[str]:
    """Check explicit business terminology, independently of structural validity.

    This is a conservative lexical check, not a claim of complete language understanding.
    Longest phrases win so "last-click revenue" is not mistaken for default revenue.
    """
    from ..fastpath import METRICS

    remaining = question.casefold().replace("_", " ")
    required = set()
    vocabulary = {**METRICS, **QUALIFIED}
    for phrase in sorted(vocabulary, key=len, reverse=True):
        pattern = rf"(?<![\w-]){phrase if phrase in QUALIFIED else re.escape(phrase)}(?![\w-])"
        if re.search(pattern, remaining):
            if vocabulary[phrase]:
                required.add(vocabulary[phrase])
            remaining = re.sub(pattern, " ", remaining)
    present = {m.metric for m in spec.measures}
    # "customers who placed orders", "buyers with an order": the orders word names who is counted, not a measure.
    if present - {"orders"} and POPULATION_ORDERS.search(question) and not ASKED_ORDERS.search(question):
        required.discard("orders")
    if len(required) > 1 and not re.search(
            r"\b(?:and|vs|versus) orders\b|\borders (?:and|vs|versus)\b", question, re.I):
        required.discard("orders")  # e.g. revenue for cash-on-delivery orders names a population
    # Rates, shares and per-order metrics mention their populations ("share of delivered orders that arrived late",
    # "RTO rate for COD orders"); do not demand extra count measures.
    if any(m.ratio or m.agg == "share" or m.metric == "aov" or (
            m.metric in cat.metrics.metrics and cat.metrics.metrics[m.metric].unit in ("ratio", "multiple"))
           for m in spec.measures):
        required -= {"orders", "sessions"}
    problems = [f"the question explicitly requests {m}; use its governed metric" for m in sorted(required - present)]
    if present & {"return_rate", "cancellation_rate"} and re.search(
            r"\b(order[- ]level|share of orders|percentage of orders|order (?:return|cancellation) rate)\b",
            question, re.I):
        problems.append("the requested order grain differs from the governed line-level rate; clarify the denominator")
    for word in ("category", "brand", "city", "channel", "device", "traffic source", "payment type"):
        field = word.replace(" ", "_")
        cut = "marketing_channel" if word == "channel" and spec.table in (
            "marketing_spend", "marketing_attribution", "sales") else field
        if re.search(rf"\bby {word}\b", question, re.I) and cut not in spec.dimensions:
            # Qualified columns may be the same governed cut.
            wanted = cat.concepts.column(cut, cat.subject_of.get(spec.table))
            if wanted and wanted.column not in spec.dimensions:
                problems.append(f"the question asks for a split by {word}")
        try:
            expression, _, entry = cat.resolve(cut, spec.table)
            values = entry.labels() if entry and entry.expression else cat.values_of(expression)
            # "COD versus prepaid" names the members of a split, not a restriction to one of them.
            compared = cut in spec.dimensions and re.search(r"\b(?:versus|vs\.?|compared? (?:with|to)|between)\b",
                                                             question, re.I)
            for value in [] if compared else values or []:
                if re.search(rf"\b(?:for|in|from|{word}(?: is| equals)?) ['\"]?{re.escape(value)}\b", question, re.I):  # noqa: SIM102
                    if not any(cat.resolve(f.field, spec.table)[0] == expression and f.op in ("eq", "in")
                               and any(str(v).casefold() == value.casefold() for v in f.values) for f in spec.filters):
                        problems.append(f"the question explicitly restricts {word} to {value}")
        except (KeyError, ValueError):
            pass  # Unknown fields are structural validation's responsibility.
    for word, grain in (("daily", "day"), ("weekly", "week"), ("monthly", "month")):
        if re.search(rf"\b{word}\b", question, re.I) and (not spec.time or spec.time.grain != grain):
            problems.append(f"the question requests {grain} aggregation")
    if last_day and (expected := fast_spec(question, cat, last_day)) is not None:
        if expected.time and (not spec.time or expected.time != spec.time):
            problems.append("the interpreted dates or grain differ from the explicit requested period")
        if expected.ranking and (not spec.ranking or expected.ranking.limit != spec.ranking.limit
                                 or expected.ranking.order != spec.ranking.order):
            problems.append("the ranking differs from the requested top/bottom count")
    dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", question)
    if dates:
        endpoints = {str(spec.time.start), str(spec.time.end)} if spec.time else set()
        if spec.operation:
            endpoints |= {str(spec.operation.previous_start), str(spec.operation.previous_end)}
        if not set(dates) <= endpoints:
            problems.append("the interpreted date bounds omit an explicit requested date")
    return problems
