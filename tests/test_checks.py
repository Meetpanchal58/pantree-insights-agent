"""Result checks in code: deterministic problems found before the LLM validator sees the result."""
import pandas as pd

from pantree_insights.checks import result_problems

SQL = "SELECT s.courier, COUNT(*) AS lines FROM sales s JOIN sellers se ON se.seller_id = s.seller_id " \
      "WHERE se.city = 'Mumbai' GROUP BY 1"
OK = pd.DataFrame({"courier": ["A", "B"], "lines": [10, 20], "rto_rate": [0.03, 0.05]})


def test_a_clean_result_has_no_problems():
    assert result_problems(OK, SQL, [("sellers.city", "Mumbai")]) == []


def test_a_resolved_filter_missing_from_the_sql_is_reported():
    problems = result_problems(OK, SQL.replace("'Mumbai'", "'Pune'"), [("sellers.city", "Mumbai")])
    assert len(problems) == 1 and "sellers.city = 'Mumbai'" in problems[0]
    # same value but on another column (the customer's city instead of the seller's) is also wrong
    wrong_column = "SELECT COUNT(*) AS n FROM sales s WHERE s.ship_city = 'Mumbai'"
    assert result_problems(pd.DataFrame({"n": [1]}), wrong_column, [("sellers.city", "Mumbai")])


def test_repeated_rows_and_empty_columns_are_reported():
    repeated = pd.concat([OK, OK.iloc[[0]]])
    assert any("repeat" in p for p in result_problems(repeated, SQL, []))
    empty_col = OK.assign(aov=[None, None])
    assert any("aov" in p and "empty" in p for p in result_problems(empty_col, SQL, []))


def test_rates_above_100_percent_point_to_double_counting():
    bad = OK.assign(rto_rate=[0.03, 1.4])
    assert any("rto_rate" in p for p in result_problems(bad, SQL, []))
    # percentages on a 0-100 scale, growth and ratios such as ROAS may exceed 1
    fine = OK.assign(return_pct=[12.0, 40.0], yoy_growth=[1.8, -0.2], roas=[6.5, 9.1], gross_margin_pct=[0.3, -0.1])
    assert result_problems(fine, SQL, []) == []
    negative = OK.assign(conversion_rate=[-0.1, 0.2])
    assert any("conversion_rate" in p for p in result_problems(negative, SQL, []))


def test_empty_results_are_left_to_the_validator():
    assert result_problems(OK.iloc[0:0], SQL, [("sellers.city", "Mumbai")]) == []


def test_same_column_name_on_the_wrong_table_is_reported():
    """customers.city and sellers.city are both 'city': the seller's city was asked for."""
    sql = ("SELECT COUNT(DISTINCT s.order_id) AS orders FROM sales s JOIN customers c ON c.customer_id = s.customer_id "
           "WHERE c.city = 'Mumbai'")
    assert result_problems(pd.DataFrame({"orders": [5]}), sql, [("sellers.city", "Mumbai")])


def test_filter_on_a_cte_column_is_accepted():
    sql = ("WITH se AS (SELECT seller_id, city FROM sellers) SELECT COUNT(DISTINCT s.order_id) AS orders "
           "FROM sales s JOIN se ON se.seller_id = s.seller_id WHERE se.city = 'Mumbai'")
    assert result_problems(pd.DataFrame({"orders": [5]}), sql, [("sellers.city", "Mumbai")]) == []


def test_a_count_named_like_a_rate_is_not_a_rate():
    """Found on the query library: 'products_for_80_pct' counts products (633)."""
    frame = pd.DataFrame({"products_for_80_pct": [633], "share_of_products": [0.205]})
    assert result_problems(frame, "SELECT 1 AS x", []) == []
