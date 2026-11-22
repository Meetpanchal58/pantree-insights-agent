"""Verified analysis templates: reviewed SQL for multi-step questions a single QuerySpec cannot express.

The interpreter picks a template by name and fills its parameters; code validates them (pydantic) and builds the
SQL. Like compiled specs, template answers are VERIFIED: the SQL is fixed, reviewed and tested against hand-written
gold answers (evals/gold_specs.yaml, tests/test_templates.py). A new recurring question shape becomes a new template.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

PAID = "('Paid Search', 'Paid Social', 'Display')"
RESULT_UNITS = {
    "event_daily_net_sales": "inr", "before_daily_net_sales": "inr", "lift": "ratio",
    "last_click_revenue": "inr", "spend": "inr", "roas_last_click": "multiple",
    "platform_orders": "count", "recorded_orders": "count", "over_reporting": "multiple",
    "repeat_share": "ratio", "buyers": "count",
    "repeat_buyers": "count", "sessions": "count", "conversion": "ratio", "customers": "count",
    "orders": "count", "realized_revenue": "inr",
}


class EventLift(BaseModel):
    days_before: int = Field(14, ge=1, le=60, description="Baseline window: days before each event's start")


class ChannelRoas(BaseModel):
    by: Literal["channel", "channel_group"] = "channel"


class RepeatWithin(BaseModel):
    days: int = Field(90, ge=7, le=365, description="Window after the first order")


class Dated(BaseModel):
    start: date
    end: date

    @model_validator(mode="after")
    def _bounded(self):
        if not 0 <= (self.end - self.start).days <= 366:
            raise ValueError("use an ordered date window of at most 367 days")
        return self


class Cohort(Dated):
    days: int = Field(90, ge=1, le=365)
    as_of: date
    grain: Literal["month", "quarter", "year"] = "month"

    @model_validator(mode="after")
    def _observation(self):
        if self.as_of < self.end:
            raise ValueError("as_of must be on or after the cohort end date")
        return self


Event = Literal["session_start", "home_view", "plp_view", "search", "pdp_view", "add_to_cart",
                "checkout_start", "payment_attempt", "purchase"]


class Funnel(Dated):
    steps: list[Event] = Field(min_length=2, max_length=6)

    @model_validator(mode="after")
    def _unique(self):
        if len(set(self.steps)) != len(self.steps):
            raise ValueError("funnel steps must be distinct")
        return self


class Segments(Dated):
    min_orders: int = Field(2, ge=2, le=100)
    recency_days: int = Field(30, ge=1, le=365)


def _orders() -> str:
    """One row per order before customer-level stages; the same governed revenue definition as specs."""
    from .semantic import load_metrics
    revenue = load_metrics().metrics["realized_revenue"].expression()
    return (f"SELECT s.customer_id, s.order_id, MIN(s.placed_at) AS placed_at, {revenue} AS realized_revenue "
            "FROM sales s WHERE s.line_type = 'Sale' GROUP BY s.customer_id, s.order_id")


def _cohort(p: Cohort) -> str:
    return f"""WITH order_rows AS ({_orders()}), firsts AS (
  SELECT customer_id, MIN(placed_at) AS first_at FROM order_rows GROUP BY 1), eligible AS (
  SELECT customer_id, first_at FROM firsts WHERE CAST(first_at AS DATE) BETWEEN DATE '{p.start}' AND DATE '{p.end}'
    AND first_at + INTERVAL {p.days} DAY < DATE '{p.as_of}' + INTERVAL 1 DAY), buyers AS (
  SELECT e.customer_id, e.first_at, COUNT(o.order_id) > 0 AS repeated
  FROM eligible e LEFT JOIN order_rows o ON e.customer_id = o.customer_id
    AND o.placed_at > e.first_at AND o.placed_at <= e.first_at + INTERVAL {p.days} DAY
  GROUP BY e.customer_id, e.first_at)
SELECT date_trunc('{p.grain}', first_at) AS cohort, COUNT(*) AS buyers,
       COUNT(*) FILTER (WHERE repeated) AS repeat_buyers,
       COUNT(*) FILTER (WHERE repeated) * 1.0 / NULLIF(COUNT(*), 0) AS repeat_share
FROM buyers GROUP BY 1 ORDER BY 1"""


def _segments(p: Segments) -> str:
    return f"""WITH order_rows AS ({_orders()}), customers AS (
  SELECT customer_id, COUNT(*) AS orders, MAX(placed_at) AS last_at, SUM(realized_revenue) AS realized_revenue
  FROM order_rows WHERE CAST(placed_at AS DATE) BETWEEN DATE '{p.start}' AND DATE '{p.end}' GROUP BY 1), segments AS (
  SELECT customer_id, orders, realized_revenue, CASE
    WHEN CAST(last_at AS DATE) < DATE '{p.end}' - INTERVAL {p.recency_days - 1} DAY THEN 'lapsed'
    WHEN orders >= {p.min_orders} THEN 'active_repeat' ELSE 'active_low_frequency' END AS segment FROM customers)
SELECT segment, COUNT(*) AS customers, SUM(orders) AS orders, SUM(realized_revenue) AS realized_revenue
FROM segments GROUP BY 1 ORDER BY 1"""


def _funnel(p: Funnel) -> str:
    ctes = [f"events AS (SELECT session_id, event_at, event_type FROM clickstream WHERE "
            f"CAST(event_at AS DATE) BETWEEN DATE '{p.start}' AND DATE '{p.end}')",
            f"step_0 AS (SELECT session_id, MIN(event_at) AS reached_at FROM events "
            f"WHERE event_type = '{p.steps[0]}' GROUP BY 1)"]
    for i, event in enumerate(p.steps[1:], 1):
        ctes.append(f"step_{i} AS (SELECT p.session_id, MIN(e.event_at) AS reached_at FROM step_{i-1} p "
                    f"JOIN events e ON e.session_id = p.session_id AND e.event_at > p.reached_at "
                    f"WHERE e.event_type = '{event}' GROUP BY 1)")
    rows = [f"SELECT '{event}' AS step, {i + 1} AS step_order, COUNT(*) AS sessions, "
            f"COUNT(*) * 1.0 / NULLIF((SELECT COUNT(*) FROM step_0), 0) AS conversion FROM step_{i}"
            for i, event in enumerate(p.steps)]
    return "WITH " + ", ".join(ctes) + " " + " UNION ALL ".join(rows) + " ORDER BY step_order"


def _event_lift(p: EventLift) -> str:
    d = p.days_before
    return f"""WITH daily AS (
  SELECT CAST(placed_at AS DATE) AS day, SUM(CASE WHEN cancelled_at IS NULL THEN net_amount ELSE 0 END) AS net_sales
  FROM sales WHERE line_type = 'Sale' GROUP BY 1)
SELECT e.event_id, e.name, CAST(e.start_date AS DATE) AS start_date, CAST(e.end_date AS DATE) AS end_date,
       COALESCE(SUM(d.net_sales) FILTER (WHERE d.day BETWEEN e.start_date AND e.end_date), 0)
         / (date_diff('day', e.start_date, e.end_date) + 1) AS event_daily_net_sales,
       COALESCE(SUM(d.net_sales) FILTER (WHERE d.day BETWEEN e.start_date - INTERVAL {d} DAY
                                            AND e.start_date - INTERVAL 1 DAY), 0) / {d} AS before_daily_net_sales,
       event_daily_net_sales / NULLIF(before_daily_net_sales, 0) - 1 AS lift
FROM sale_events e LEFT JOIN daily d ON d.day BETWEEN e.start_date - INTERVAL {d} DAY AND e.end_date
WHERE e.end_date <= (SELECT MAX(day) FROM daily)
  AND e.start_date - INTERVAL {d} DAY >= (SELECT MIN(day) FROM daily)
GROUP BY e.event_id, e.name, e.start_date, e.end_date
ORDER BY lift DESC"""


def _channel_roas(p: ChannelRoas) -> str:
    by = p.by
    return f"""WITH revenue AS (
  SELECT c.{by}, SUM(a.credit_last_click * a.order_revenue) AS last_click_revenue
  FROM marketing_attribution a JOIN campaigns c ON c.campaign_id = a.campaign_id GROUP BY 1),
spend AS (
  SELECT c.{by}, SUM(m.spend) AS spend
  FROM marketing_spend m JOIN campaigns c ON c.campaign_id = m.campaign_id GROUP BY 1)
SELECT s.{by}, r.last_click_revenue, s.spend, r.last_click_revenue / NULLIF(s.spend, 0) AS roas_last_click
FROM spend s LEFT JOIN revenue r ON r.{by} = s.{by}
ORDER BY roas_last_click DESC NULLS LAST"""


def _platform_vs_recorded(p: ChannelRoas) -> str:
    by = p.by
    return f"""WITH platform AS (
  SELECT c.{by}, SUM(m.platform_conversions) AS platform_orders
  FROM marketing_spend m JOIN campaigns c ON c.campaign_id = m.campaign_id
  WHERE c.channel_group IN {PAID} GROUP BY 1),
shop AS (
  SELECT c.{by}, COUNT(DISTINCT s.order_id) AS recorded_orders
  FROM sales s JOIN campaigns c ON c.campaign_id = s.campaign_id
  WHERE s.line_type = 'Sale' GROUP BY 1)
SELECT p.{by}, p.platform_orders, sh.recorded_orders,
       p.platform_orders * 1.0 / NULLIF(sh.recorded_orders, 0) AS over_reporting
FROM platform p LEFT JOIN shop sh ON sh.{by} = p.{by}
ORDER BY over_reporting DESC NULLS LAST"""


def _repeat_within(p: RepeatWithin) -> str:
    d = p.days
    return f"""WITH firsts AS (
  SELECT customer_id, MIN(placed_at) AS first_at FROM sales WHERE line_type = 'Sale' GROUP BY 1),
eligible AS (
  SELECT customer_id, first_at FROM firsts
  WHERE first_at <= (SELECT MAX(placed_at) FROM sales) - INTERVAL {d} DAY),
again AS (
  SELECT DISTINCT e.customer_id FROM eligible e JOIN sales s ON s.customer_id = e.customer_id
  WHERE s.line_type = 'Sale' AND s.placed_at > e.first_at AND s.placed_at <= e.first_at + INTERVAL {d} DAY)
SELECT COUNT(a.customer_id) * 1.0 / COUNT(*) AS repeat_share, COUNT(*) AS buyers
FROM eligible e LEFT JOIN again a ON a.customer_id = e.customer_id"""


@dataclass(frozen=True)
class Template:
    description: str
    params: type[BaseModel]
    build: Callable
    readback: Callable[[BaseModel], str]


TEMPLATES: dict[str, Template] = {
    "cohort_retention": Template(
        "Repeat purchase within N days, grouped by first-ever order cohort; only mature buyers", Cohort, _cohort,
        lambda p: f"First-order {p.grain} cohorts from {p.start} to {p.end}; later order within {p.days} days, "
                  f"observed through {p.as_of}; immature buyers excluded; all placed orders including cancellations"),
    "session_funnel": Template(
        "Strictly time-ordered events in the same session within the requested dates", Funnel, _funnel,
        lambda p: f"Sessions reaching {' then '.join(p.steps)} from {p.start} to {p.end}; "
                  "each event must be strictly later; conversion denominator is sessions reaching the first step"),
    "customer_segments": Template(
        "Customer frequency and recency segments among buyers in a bounded period", Segments, _segments,
        lambda p: f"Buyers from {p.start} to {p.end}; lapsed if no order in the last {p.recency_days} days; "
                  f"otherwise active repeat at {p.min_orders} orders or active low frequency; "
                  "all placed orders count, revenue uses the governed realized definition"),
    "event_lift": Template(
        "Lift of each sale event: average daily net sales during the event vs the days before it", EventLift,
        _event_lift, lambda p: f"For each finished sale event: average daily net sales during the event, compared "
                               f"with the {p.days_before} calendar days before it, including zero-sales days; "
                               "only windows within the recorded sales date range (lift = event / before - 1)"),
    "channel_roas": Template(
        "Return on ad spend per channel using the shop's own last-click attribution (credited revenue / spend)",
        ChannelRoas, _channel_roas,
        lambda p: f"For each {p.by.replace('_', ' ')}: last-click credited revenue divided by marketing spend"),
    "platform_vs_recorded_orders": Template(
        "Orders ad platforms report vs orders the shop recorded per paid channel (over-reporting)",
        ChannelRoas, _platform_vs_recorded,
        lambda p: f"For each paid {p.by.replace('_', ' ')}: orders the ad platforms claim vs orders recorded in sales"),
    "repeat_within_days": Template(
        "Share of buyers who ordered again within N days of their first order", RepeatWithin, _repeat_within,
        lambda p: f"Share of buyers who placed another order within {p.days} days of their first order "
                  f"(buyers whose first order is at least {p.days} days before the data's last day)"),
}


class TemplateError(ValueError):
    """Unknown template or invalid parameters; phrased for the user."""


def build(name: str, params: dict | None) -> tuple[str, str]:
    """(SQL, read-back) for a template call."""
    if name not in TEMPLATES:
        raise TemplateError(f"unknown analysis '{name}'")
    t = TEMPLATES[name]
    unknown = set(params or {}) - set(t.params.model_fields)
    if unknown:
        raise TemplateError(f"{name}: unsupported parameters {', '.join(sorted(unknown))}")
    try:
        p = t.params.model_validate(params or {})
    except ValidationError as error:
        raise TemplateError(f"{name}: {error.errors()[0]['msg']}") from error
    return t.build(p), t.readback(p)


def describe() -> str:
    """For the interpreter's prompt."""
    lines = []
    for name, t in TEMPLATES.items():
        fields = ", ".join(f"{k} ({'required' if f.is_required() else f'default {f.default}'})"
                           for k, f in t.params.model_fields.items())
        lines.append(f"- {name}: {t.description}. Parameters: {fields}")
    return "\n".join(lines)
