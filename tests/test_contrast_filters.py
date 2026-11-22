"""Deterministic filters on the contrast check: the alternatives from the previous paid evaluation that stopped
correct answers, and alternatives that must still be asked about. No database, no model."""
import pytest

from pantree_insights.interpret.readings import immaterial
from pantree_insights.spec import QuerySpec

Y25 = {"start": "2025-01-01", "end": "2025-12-31"}
PAID = {"field": "channel_group", "op": "in", "values": ["Paid Search", "Paid Social", "Display"]}


def spec(**kw):
    return QuerySpec.model_validate({"table": "sales", **kw})


@pytest.mark.parametrize("question, primary, alternative", [
    ("What did one ad click cost on each paid channel in 2025?",            # holdout x06
     dict(table="marketing_spend", measures=[{"name": "cpc", "metric": "cpc"}], dimensions=["marketing_channel"],
          filters=[PAID], time=Y25),
     dict(table="marketing_spend", measures=[{"name": "cpc", "metric": "cpc"}], dimensions=["channel_group"],
          filters=[PAID], time=Y25)),
    ("What was realized revenue in each region of the delivery address in 2025?",  # holdout x01
     dict(measures=[{"name": "r", "metric": "realized_revenue"}], dimensions=["region"], time=Y25),
     dict(measures=[{"name": "r", "metric": "realized_revenue"}], dimensions=["region"],
          time={**Y25, "field": "sales.delivered_at"})),
    ("Which couriers deliver late most often?",                              # regression h06
     dict(measures=[{"name": "late", "metric": "late_delivery_rate"}], dimensions=["courier"]),
     dict(measures=[{"name": "late", "agg": "count", "where": [
         {"field": "sales.delivered_at", "op": "gt_field", "other": "sales.promised_delivery_date"}]}],
         dimensions=["courier"])),
    ("For apparel delivered in 2026, what share of lines fitted, were too small or were too large?",  # coverage c18
     dict(measures=[{"name": "lines", "agg": "count"}], dimensions=["size_fit"],
          filters=[{"field": "category", "op": "eq", "values": ["Apparel"]}], shares=["lines"]),
     dict(measures=[{"name": "lines", "agg": "count"}], dimensions=["size_fit"],
          filters=[{"field": "category", "op": "eq", "values": ["Apparel"]},
                   {"field": "sales.size_fit", "op": "is_set"}], shares=["lines"])),
    ("What is the return rate by category?",                                 # a supporting count is presentation
     dict(measures=[{"name": "rr", "metric": "return_rate"}], dimensions=["category"]),
     dict(measures=[{"name": "rr", "metric": "return_rate"}, {"name": "n", "agg": "count"}],
          dimensions=["category"])),
])
def test_alternatives_the_question_settles_are_not_asked(question, primary, alternative):
    assert immaterial(question, spec(**primary), spec(**alternative))


@pytest.mark.parametrize("question, primary, alternative", [
    ("Revenue by gender",                                                    # whose gender: both readings fit
     dict(measures=[{"name": "r", "metric": "realized_revenue"}], dimensions=["customer_gender"]),
     dict(measures=[{"name": "r", "metric": "realized_revenue"}], dimensions=["product_gender"])),
    ("How many orders were delivered in 2025?",                              # the question names the milestone
     dict(measures=[{"name": "o", "metric": "orders"}], time=Y25),
     dict(measures=[{"name": "o", "metric": "orders"}], time={**Y25, "field": "sales.delivered_at"})),
    ("Which brands perform best?",                                           # a different governed metric
     dict(measures=[{"name": "r", "metric": "realized_revenue"}], dimensions=["brand"]),
     dict(measures=[{"name": "o", "metric": "orders"}], dimensions=["brand"])),
])
def test_materially_different_readings_are_still_asked(question, primary, alternative):
    assert immaterial(question, spec(**primary), spec(**alternative)) is None
