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


@pytest.mark.data
def test_a_subtly_wrong_query_is_caught(con):
    """Net sales that forgets to exclude cancelled lines (a typical LLM slip) disagrees with the definition."""
    from pantree_insights.recompute import compare

    wrong = ("SELECT p.category, SUM(s.net_amount) AS net_sales FROM sales s "
             "JOIN product_master p ON p.product_id = s.product_id WHERE s.line_type = 'Sale' GROUP BY 1")
    canonical = run_query(con, canonical_sql("net_sales", GRAPH, by=["product_master.category"])).frame
    problems = compare(run_query(con, wrong).frame, canonical, ["category"], "net_sales")
    assert problems and "recomputed" in problems[0]
    right = wrong.replace("SUM(s.net_amount)", "SUM(CASE WHEN s.cancelled_at IS NULL THEN s.net_amount ELSE 0 END)")
    assert compare(run_query(con, right).frame, canonical, ["category"], "net_sales") == []


def test_keys_that_differ_only_in_case_are_different_groups():
    """Found on the full shop: 'ADIDAS' and 'Adidas' are separate brand values and must not be merged."""
    import pandas as pd

    from pantree_insights.recompute import compare

    result = pd.DataFrame({"brand": ["ADIDAS", "Adidas"], "units": [10, 20]})
    canonical = pd.DataFrame({"brand": ["ADIDAS", "Adidas"], "value": [10, 20]})
    assert compare(result, canonical, ["brand"], "units") == []
    swapped = pd.DataFrame({"brand": ["ADIDAS", "Adidas"], "value": [20, 10]})
    assert len(compare(result, canonical.assign(value=swapped["value"]), ["brand"], "units")) == 2


# ----------------------------------------------------------------------------- runtime: the agent's answer
def test_answer_alignment_by_values_whatever_the_column_names():
    import pandas as pd

    from pantree_insights.recompute import check_answer

    canonical = pd.DataFrame({"category": ["Apparel", "Footwear"], "value": [100.0, 50.0]})
    agent = pd.DataFrame({"cat": ["Footwear", "Apparel"], "lines": [7, 9], "net": [50.2, 99.8]})
    assert check_answer(agent, canonical, ["category"]) == ("verified", [])
    wrong = agent.assign(net=[60.0, 99.8])
    status, problems = check_answer(wrong, canonical, ["category"])
    assert status == "mismatch" and "Footwear" in problems[0] and "50" in problems[0]
    top1 = agent.iloc[[1]]                                              # top-N: a subset of the rows
    assert check_answer(top1, canonical, ["category"])[0] == "verified"
    in_percent = agent.assign(net=[5020.0, 9980.0])                     # not a ratio: x100 is a mismatch
    assert check_answer(in_percent, canonical, ["category"])[0] == "mismatch"


def test_answer_that_cannot_be_aligned_is_unverified_not_wrong():
    import pandas as pd

    from pantree_insights.recompute import check_answer

    canonical = pd.DataFrame({"category": ["Apparel", "Footwear"], "value": [100.0, 50.0]})
    by_month = pd.DataFrame({"month": ["2026-01", "2026-02"], "net": [70.0, 80.0]})   # a different breakdown
    assert check_answer(by_month, canonical, ["category"])[0] == "unverified"
    single = pd.DataFrame({"total": [150.0]})
    assert check_answer(single, pd.DataFrame({"value": [150.0]}), []) == ("verified", [])
    assert check_answer(single, pd.DataFrame({"value": [120.0]}), [])[0] == "mismatch"


def test_ratio_in_percent_is_accepted():
    import pandas as pd

    from pantree_insights.recompute import check_answer

    canonical = pd.DataFrame({"category": ["Apparel"], "value": [0.253]})
    agent = pd.DataFrame({"category": ["Apparel"], "return_pct": [25.3]})
    assert check_answer(agent, canonical, ["category"], ratio=True)[0] == "verified"
