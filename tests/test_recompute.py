"""Canonical recomputation: metric SQL built by code from metrics.yaml must reproduce verified answers."""
import sys
from pathlib import Path

import pytest

from pantree_insights.db import run_query
from pantree_insights.guardrails import check_sql
from pantree_insights.joins import JoinGraph
from pantree_insights.recompute import canonical_sql
from pantree_insights.semantic import load_examples, load_schema

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
from scoring import align  # noqa: E402
from test_joins import fake_profile  # noqa: E402

GRAPH = JoinGraph(load_schema(), fake_profile())
EXAMPLES = {e.id: e for e in load_examples().examples}


def test_builds_sql_with_full_table_names_and_code_joins():
    sql = canonical_sql("net_sales", GRAPH, by=["sellers.city"], filters=[("product_master.category", "Footwear")],
                        period=("2026-01-01", "2026-11-14"))
    assert "JOIN sellers ON sales.seller_id = sellers.seller_id" in sql
    assert "JOIN product_master ON sales.product_id = product_master.product_id" in sql
    assert "sales.line_type = 'Sale'" in sql and "product_master.category = 'Footwear'" in sql
    assert "sales.placed_at < TIMESTAMP '2026-11-15'" in sql                 # inclusive end date
    assert check_sql(sql).ok, check_sql(sql).errors


def test_refuses_what_it_cannot_recompute_safely():
    assert canonical_sql("support_tickets", GRAPH) is None                   # source is a subquery
    assert canonical_sql("roas_last_click", GRAPH) is None                   # custom metric
    assert canonical_sql("orders", GRAPH, by=["marketing_attribution.channel_group"]) is None  # would repeat lines


def test_left_joins_keep_rows_without_a_partner():
    """81% of sale lines have no review: an inner join would silently drop them from the totals."""
    sql = canonical_sql("net_sales", GRAPH, by=["product_reviews.aspect"])
    assert "LEFT JOIN product_reviews ON" in sql


@pytest.mark.data
@pytest.mark.parametrize(("example", "metric", "by"), [
    ("e06", "realized_revenue", []),
    ("m03", "realized_revenue", ["product_master.category"]),
    ("m04", "return_rate", ["product_master.category"]),
    ("e04", "orders", ["sales.payment_method"]),
    ("m09", "marketing_spend", ["campaigns.channel_group", "campaigns.channel"]),
])
def test_reproduces_verified_examples(con, example, metric, by):
    gold = run_query(con, check_sql(EXAMPLES[example].sql).sql).frame
    mine = run_query(con, check_sql(canonical_sql(metric, GRAPH, by=by)).sql).frame
    keys = [c.split(".")[1] for c in by]
    ok, reason, _ = align(mine, gold, keys, ["value"])
    assert ok, reason
