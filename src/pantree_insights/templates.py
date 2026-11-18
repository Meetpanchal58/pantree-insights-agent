"""Verified analysis templates: reviewed SQL for multi-step questions a single QuerySpec cannot express.

The interpreter picks a template by name and fills its parameters; code validates them (pydantic) and builds the
SQL. Like compiled specs, template answers are VERIFIED: the SQL is fixed, reviewed and tested against hand-written
gold answers (evals/gold_specs.yaml, tests/test_templates.py). A new recurring question shape becomes a new template.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

PAID = "('Paid Search', 'Paid Social', 'Display')"


class EventLift(BaseModel):
    days_before: int = Field(14, ge=1, le=60, description="Baseline window: days before each event's start")


class ChannelRoas(BaseModel):
    by: Literal["channel", "channel_group"] = "channel"


class PlatformVsRecorded(BaseModel):
    by: Literal["channel", "channel_group"] = "channel"


class RepeatWithin(BaseModel):
    days: int = Field(90, ge=7, le=365, description="Window after the first order")


def _event_lift(p: EventLift) -> str:
    d = p.days_before
    return f"""WITH daily AS (
  SELECT CAST(placed_at AS DATE) AS day, SUM(CASE WHEN cancelled_at IS NULL THEN net_amount ELSE 0 END) AS net_sales
  FROM sales WHERE line_type = 'Sale' GROUP BY 1)
SELECT e.event_id, e.name, CAST(e.start_date AS DATE) AS start_date, CAST(e.end_date AS DATE) AS end_date,
       AVG(d.net_sales) FILTER (WHERE d.day BETWEEN e.start_date AND e.end_date) AS event_daily_net_sales,
       AVG(d.net_sales) FILTER (WHERE d.day BETWEEN e.start_date - INTERVAL {d} DAY
                                            AND e.start_date - INTERVAL 1 DAY) AS before_daily_net_sales,
       event_daily_net_sales / NULLIF(before_daily_net_sales, 0) - 1 AS lift
FROM sale_events e JOIN daily d ON d.day BETWEEN e.start_date - INTERVAL {d} DAY AND e.end_date
WHERE e.end_date <= (SELECT MAX(placed_at) FROM sales)
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


def _platform_vs_recorded(p: PlatformVsRecorded) -> str:
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
    "event_lift": Template(
        "Lift of each sale event: average daily net sales during the event vs the days before it", EventLift,
        _event_lift, lambda p: f"For each finished sale event: average daily net sales during the event, compared "
                               f"with the {p.days_before} days before it (lift = event / before - 1)"),
    "channel_roas": Template(
        "Return on ad spend per channel using the shop's own last-click attribution (credited revenue / spend)",
        ChannelRoas, _channel_roas,
        lambda p: f"For each {p.by.replace('_', ' ')}: last-click credited revenue divided by marketing spend"),
    "platform_vs_recorded_orders": Template(
        "Orders ad platforms report vs orders the shop recorded per paid channel (over-reporting)",
        PlatformVsRecorded, _platform_vs_recorded,
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
    try:
        p = t.params.model_validate(params or {})
    except ValidationError as error:
        raise TemplateError(f"{name}: {error.errors()[0]['msg']}") from error
    return t.build(p), t.readback(p)


def describe() -> str:
    """For the interpreter's prompt."""
    lines = []
    for name, t in TEMPLATES.items():
        fields = ", ".join(f"{k} (default {f.default})" for k, f in t.params.model_fields.items())
        lines.append(f"- {name}: {t.description}. Parameters: {fields}")
    return "\n".join(lines)
