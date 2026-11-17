"""Root causes found in the stopped production run (Nov 17), each fixed in code and locked here.

1. a date split ("monthly") or an unknown cut made the number check reject correct answers 3 times
2. category columns without a concept were dropped from the understanding silently
3. "COD vs prepaid" could not be expressed as one cut
4. business words ("on sale") were treated as missing stored values and forced a question even with defaults
5. names several IDs share (campaign, event, product names) merged different rows when grouped
"""
import sys
from pathlib import Path

import pandas as pd

from pantree_insights.agents import Mention, QuestionFrame
from pantree_insights.catalog import describe, label_columns
from pantree_insights.checks import label_grain_problems
from pantree_insights.joins import JoinGraph
from pantree_insights.recompute import check_answer
from pantree_insights.semantic import load_concepts, load_schema
from pantree_insights.understanding import readback, resolve

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_joins import fake_profile  # noqa: E402

CONCEPTS = load_concepts()
GRAPH = JoinGraph(load_schema(), fake_profile())
VALUES = {"COD": ["sales.payment_method"], "UPI": ["sales.payment_method"], "Mumbai": ["sales.ship_city"]}
TERMS = {"on sale": "listed today (product_master.is_active = true)", "prepaid": "any method other than COD"}


def frame(**kw) -> QuestionFrame:
    return QuestionFrame(**{"restated_question": "q", **kw})


def run(f):
    return resolve(f, CONCEPTS, GRAPH, VALUES, [], {}, {}, TERMS)


# 1 -------------------------------------------------------------------------------------------------------------
def test_more_rows_than_the_confirmed_grain_is_unverified_not_rejected():
    monthly = pd.DataFrame({"month": pd.to_datetime(["2025-01-01", "2025-02-01"]), "orders": [10, 12]})
    total = pd.DataFrame({"value": [22]})
    assert check_answer(monthly, total, [], column="orders") == ("unverified", [])


def test_month_grain_is_verified_and_wrong_numbers_still_rejected():
    agent = pd.DataFrame({"month": pd.to_datetime(["2025-01-01", "2025-02-01"]), "orders": [10, 12]})
    canonical = pd.DataFrame({"month": pd.to_datetime(["2025-01-01", "2025-02-01"]), "value": [10, 12]})
    assert check_answer(agent, canonical, ["month"], column="orders")[0] == "verified"
    wrong = agent.assign(orders=[10, 15])
    assert check_answer(wrong, canonical, ["month"], column="orders")[0] == "mismatch"


def test_unknown_label_matches_the_empty_group():
    agent = pd.DataFrame({"return_reason": ["Size Issue", "Unknown"], "n": [5, 2]})
    canonical = pd.DataFrame({"return_reason": ["Size Issue", None], "value": [5, 2]})
    assert check_answer(agent, canonical, ["return_reason"], column="n")[0] == "verified"


def test_time_grain_reaches_the_understanding_and_readback():
    f = frame(subjects=["orders"], measures=["orders"], time_grain="month")
    res = run(f)
    assert "date_trunc('month', sales.placed_at) AS month" in res.to_prompt("all data", f.time_grain)
    assert "month" in readback(f, res, CONCEPTS, type("M", (), {"metrics": {}, "custom_metrics": {}})())


# 2 -------------------------------------------------------------------------------------------------------------
def test_every_category_column_of_a_subject_table_is_a_cut():
    for column in ("order_status", "cancel_reason", "return_type"):
        assert CONCEPTS.column(column, "orders").column == f"sales.{column}"
    assert CONCEPTS.column("surface", "listings").column == "impressions.surface"
    assert CONCEPTS.column("list_position", "listings").column == "impressions.position"
    assert CONCEPTS.column("city", "orders").column == "sales.ship_city"        # hand-written concepts win


def test_unknown_cut_is_noted_not_dropped():
    res = run(frame(subjects=["orders"], breakdowns=["moon_phase"]))
    assert any("moon phase" in n for n in res.notes)


# 3 -------------------------------------------------------------------------------------------------------------
def test_cod_versus_prepaid_becomes_the_payment_type_cut():
    f = frame(subjects=["orders"], measures=["rto_rate"], breakdowns=["payment_method"], comparison="COD vs prepaid",
              mentions=[Mention(text="COD", concept="payment_method", role="comparison"),
                        Mention(text="prepaid", concept="payment_method", role="comparison")])
    view = run(f).subjects[0]
    assert view.breakdowns == [("payment_type", "sales.payment_method")] and not view.filters
    assert view.breakdown_sql() == {"payment_type": CONCEPTS.column("payment_type", "orders").expression}


def test_prepaid_orders_filter_on_the_derived_cut():
    view = run(frame(subjects=["orders"], mentions=[Mention(text="prepaid", concept="payment_method")])).subjects[0]
    assert [(f.value, f.sql()) for f in view.filters] == [
        ("Prepaid", "CASE WHEN sales.payment_method = 'COD' THEN 'COD' ELSE 'Prepaid' END = 'Prepaid'")]


# 4 -------------------------------------------------------------------------------------------------------------
def test_business_term_is_not_a_missing_value():
    res = run(frame(subjects=["catalogue"], mentions=[Mention(text="on sale", concept="other")]))
    assert not res.open_points and any("business term" in n for n in res.notes)


def test_a_real_unknown_value_is_still_asked():
    res = run(frame(subjects=["orders"], mentions=[Mention(text="Atlantis", concept="city")]))
    assert res.open_points and res.open_points[0].required


# 5 -------------------------------------------------------------------------------------------------------------
def test_names_several_ids_share_are_known_and_shown():
    labels = label_columns()
    assert labels["campaigns.campaign_name"] == "campaign_id" and labels["sale_events.name"] == "event_id"
    assert "product_master.brand_name" not in labels                 # a brand is a thing in itself
    assert "NOT unique" in describe(["campaigns"])


def test_grouping_by_a_shared_name_is_rejected_unless_told_apart():
    merged = "SELECT c.campaign_name, SUM(a.order_revenue) FROM marketing_attribution a " \
             "JOIN campaigns c ON c.campaign_id = a.campaign_id GROUP BY 1"
    assert label_grain_problems(merged)
    assert not label_grain_problems(merged.replace("GROUP BY 1", "GROUP BY a.campaign_id, 1"))
    assert not label_grain_problems("SELECT name, CAST(start_date AS DATE), COUNT(*) FROM sale_events GROUP BY 1, 2")


# diagnostic run (Nov 17): m08, m11, h02 ---------------------------------------------------------------------------
def test_plural_business_term_is_not_a_missing_value():
    terms = {**TERMS, "active product": "listed today (product_master.is_active = true)"}
    res = resolve(frame(subjects=["stock_now"], mentions=[Mention(text="active products", concept="other")]),
                  CONCEPTS, GRAPH, VALUES, [], {}, {}, terms)
    assert not res.open_points


def test_a_ranking_may_return_every_group():
    canonical = pd.DataFrame({"issue": list("abcdefghijkl"), "value": range(12, 0, -1)})
    everything = canonical.rename(columns={"value": "n"})
    assert check_answer(everything, canonical, ["issue"], column="n", limit=10, ranking="highest")[0] == "verified"
    top3 = everything.head(3)
    assert check_answer(top3, canonical, ["issue"], column="n", limit=10, ranking="highest")[0] == "mismatch"
    skipped = everything.drop(index=0)                  # the true number one is missing
    assert check_answer(skipped, canonical, ["issue"], column="n", limit=10, ranking="highest")[0] == "mismatch"


def test_sale_event_is_joined_by_its_date_window_not_event_id():
    f = frame(subjects=["orders"], measures=["net_sales"], breakdowns=["sale_event"], comparison="Diwali 2025",
              mentions=[Mention(text="Diwali 2026", concept="sale_event", role="comparison"),
                        Mention(text="Diwali 2025", concept="sale_event", role="comparison")])
    view = run(f).subjects[0]
    assert "sales.placed_at >= sale_events.start_date" in view.joins
    assert "sales.event_id = sale_events.event_id" not in view.joins


# production run (Nov 17) ----------------------------------------------------------------------------------------
def test_sale_event_days_versus_normal_days_is_the_day_type_cut():
    f = frame(subjects=["orders"], measures=["avg_discount_pct"], breakdowns=["sale_event"],
              comparison="normal days",
              mentions=[Mention(text="sale-event days", concept="sale_event", role="comparison"),
                        Mention(text="normal days", concept="sale_event", role="comparison")])
    view = run(f).subjects[0]
    assert [c for c, _ in view.breakdowns] == ["day_type"] and "sale_events" not in view.joins


def test_a_word_from_the_users_answer_is_not_a_missing_value():
    f = frame(subjects=["ad_spend"], mentions=[Mention(text="Last-click", concept="other")])
    res = resolve(f, CONCEPTS, GRAPH, VALUES, [], {}, {}, TERMS, ["Last-click (shop-measured)"])
    assert not res.open_points


def test_a_value_inside_the_measure_satisfies_the_filter_check():
    from pantree_insights.checks import result_problems
    sql = ("SELECT p.category, COUNT(*) FILTER (WHERE s.return_reason = 'Size Issue') * 1.0 / COUNT(*) AS r "
           "FROM sales s JOIN product_master p ON p.product_id = s.product_id WHERE s.line_type = 'Sale' GROUP BY 1")
    assert not result_problems(None, sql, [("sales.return_reason", "Size Issue")])
    plain = "SELECT p.category, COUNT(*) FROM sales s JOIN product_master p ON p.product_id = s.product_id GROUP BY 1"
    assert result_problems(None, plain, [("sales.return_reason", "Size Issue")])


def test_count_claims_are_verified():
    from pantree_insights.grounding import Claim, verify
    rows = pd.DataFrame({"sku_id": ["A", "B", "C"], "inbound_qty": [1, 0, 2]})
    ok = Claim(id="c1", value=3, unit="count", column="sku_id", row=[], op="count")
    assert verify(rows, [ok], {}).ok
    assert not verify(rows, [ok.model_copy(update={"value": 4})], {}).ok
