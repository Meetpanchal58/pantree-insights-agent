"""Counterexamples from the stakeholder-flow review; no provider calls or downloaded data."""
from types import SimpleNamespace

import pandas as pd
import pytest

from pantree_insights import agents
from pantree_insights.checks import result_problems
from pantree_insights.graph import Agent, Features
from pantree_insights.llm import FakeLLM
from pantree_insights.recompute import check_answer
from pantree_insights.understanding import _find_value


@pytest.mark.parametrize("sql", [
    "SELECT ship_city = 'Mumbai' AS label, COUNT(*) AS n FROM sales GROUP BY 1",
    "SELECT COUNT(*) AS n FROM sales WHERE ship_city = 'Mumbai' OR payment_method = 'COD'",
    "SELECT COUNT(*) AS n FROM sales WHERE NOT (ship_city = 'Mumbai')",
    "SELECT COUNT(*) AS n FROM sales WHERE ship_city IN ('Mumbai', 'Pune')",
    "WITH unused AS (SELECT ship_city FROM sales WHERE ship_city = 'Mumbai') SELECT COUNT(*) AS n FROM sales",
    "SELECT COUNT(*) AS n FROM sales s LEFT JOIN sellers se ON se.seller_id=s.seller_id AND se.city='Mumbai'",
    "WITH se AS (SELECT seller_id, city FROM sellers WHERE city='Mumbai') "
    "SELECT COUNT(*) AS n FROM sales s LEFT JOIN se ON s.seller_id=se.seller_id",
])
def test_filter_comparisons_must_restrict_contributing_rows(sql):
    column = "sellers.city" if "seller" in sql else "sales.ship_city"
    assert result_problems(pd.DataFrame({"n": [1]}), sql, [(column, "Mumbai")])


@pytest.mark.parametrize("where", ["ship_city IN ('Mumbai', 'Pune')", "ship_city='Mumbai' OR ship_city='Pune'"])
def test_multi_value_filter_is_a_union_of_selected_values(where):
    sql = f"SELECT COUNT(*) AS n FROM sales WHERE {where}"
    assert not result_problems(pd.DataFrame({"n": [2]}), sql,
                               [("sales.ship_city", "Mumbai"), ("sales.ship_city", "Pune")])


def test_nested_cte_renaming_keeps_filter_lineage():
    sql = ("WITH x AS (SELECT ship_city AS destination FROM sales), y AS (SELECT destination FROM x) "
           "SELECT COUNT(*) AS n FROM y WHERE destination='Mumbai'")
    assert not result_problems(pd.DataFrame({"n": [1]}), sql, [("sales.ship_city", "Mumbai")])


def test_unfiltered_union_branch_does_not_count_as_filtered():
    sql = ("SELECT ship_city FROM sales WHERE ship_city='Mumbai' UNION ALL SELECT ship_city FROM sales")
    assert result_problems(pd.DataFrame({"n": [1]}), sql, [("sales.ship_city", "Mumbai")])


def test_empty_result_does_not_bypass_filter_checks():
    assert result_problems(pd.DataFrame(), "SELECT ship_city FROM sales WHERE 1=0", [("sales.ship_city", "Mumbai")])


def test_wrong_metric_cannot_borrow_an_unrelated_numeric_column():
    canonical = pd.DataFrame({"category": ["Apparel", "Footwear"], "value": [100., 50.]})
    actual = pd.DataFrame({"category": ["Apparel", "Footwear"], "realized_revenue": [999., 999.],
                           "unrelated": [100., 50.]})
    assert check_answer(actual, canonical, ["category"], column="realized_revenue")[0] == "mismatch"
    assert check_answer(actual, canonical, ["category"])[0] == "unverified"
    assert check_answer(actual.head(1).assign(realized_revenue=100.), canonical, ["category"],
                        column="realized_revenue")[0] == "mismatch"
    assert check_answer(actual.head(1).assign(realized_revenue=100.), canonical, ["category"],
                        column="realized_revenue", limit=1, ranking="highest")[0] == "verified"
    assert check_answer(actual.tail(1).assign(realized_revenue=50.), canonical, ["category"],
                        column="realized_revenue", limit=1, ranking="highest")[0] == "mismatch"


def test_exact_case_wins_and_case_collisions_do_not_pick_a_winner():
    values = {"ADIDAS": ["product_master.brand_name"], "Adidas": ["product_master.brand_name"]}
    assert _find_value("ADIDAS", values, {}) == "ADIDAS"
    assert _find_value("Adidas", values, {}) == "Adidas"
    assert _find_value("adidas", values, {}) is None


def test_new_planner_questions_survive_an_understanding_summary():
    question = {"question": "Revenue or profit?", "options": ["Revenue", "Profit"],
                "default": "Revenue", "why": "different business measures"}
    llm = FakeLLM(lambda *args: {"decision": "clarify", "restated_question": "Performance",
                                 "clarifying_questions": [question]})
    data = {"first_day": "2025-01-01", "last_day": "2026-11-16", "size": "small"}
    result = agents.plan(llm, "How are we performing?", "", "", data, understanding="Subject: orders")
    assert result.decision == "clarify" and result.clarifying_questions
    result = agents.plan(llm, "How are we performing?", "", "", data, answers={question["question"]: "Profit"})
    assert result.decision == "proceed" and not result.clarifying_questions


@pytest.mark.parametrize("extra", [
    {"verification": {"status": "mismatch"}},
    {"warnings": ["Result check: missing filter"]},
    {"warnings": ["Numbers not confirmed: wrong denominator"]},
    {"result": {"truncated": True}},
])
def test_invalid_insights_do_not_enter_memory_or_close_gaps(extra):
    agent = object.__new__(Agent)
    agent.features = Features()
    agent.snapshot = None
    agent.mem = lambda state: SimpleNamespace()  # Any save/close call would fail this test.
    state = {"analysis": {}, "grounded": True, **extra}
    assert agent.remember(state)["insight_id"] is None


def test_provider_failures_have_safe_retry_messages():
    from pantree_insights.web import failure_details

    error = RuntimeError("provider details and confidential headers")
    error.status_code = 429
    error.response = SimpleNamespace(headers={"retry-after": "15"})
    details = failure_details(error)
    assert details["error_code"] == "rate_limit" and details["retry_after"] == 15
    assert details["retryable"] and "confidential" not in details["message"]
    assert failure_details(TimeoutError())["error_code"] == "timeout"


def test_reused_cte_alias_cannot_borrow_another_alias_filter():
    sql = ("WITH cities AS (SELECT city, seller_id FROM sellers) "
           "SELECT COUNT(*) AS n FROM cities a CROSS JOIN cities b WHERE a.city='Mumbai'")
    assert result_problems(pd.DataFrame({"n": [1]}), sql, [("sellers.city", "Mumbai")])
