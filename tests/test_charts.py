import pandas as pd

from pantree_insights.charts import build_chart


def test_kpi_for_single_row():
    assert build_chart(pd.DataFrame({"aov": [1796.9]}))["kind"] == "kpi"


def test_line_for_time_series():
    frame = pd.DataFrame({"month": pd.date_range("2025-01-01", periods=6, freq="MS"), "orders": range(6),
                          "realized_revenue": [1.0, 2, 3, 4, 5, 6]})
    chart = build_chart(frame, "Revenue")
    assert chart["kind"] == "line" and chart["metric"] == "realized_revenue"


def test_bar_for_category_and_ratio_axis_as_percent():
    frame = pd.DataFrame({"category": ["Footwear", "Apparel"], "return_rate": [0.26, 0.23], "delivered_lines": [9, 8]})
    chart = build_chart(frame)
    assert chart["kind"] == "bar" and chart["figure"]["layout"]["yaxis"]["tickformat"] == ".1%"


def test_year_columns_become_grouped_bars_and_month_numbers_are_labels():
    frame = pd.DataFrame({"placement_month": [1, 2, 3], "realized_revenue_2025": [1.0, 2, 3], "orders_2025": [1, 2, 3],
                          "realized_revenue_2026": [2.0, 3, 4], "orders_2026": [2, 3, 4]})
    chart = build_chart(frame, metric="realized_revenue_2026")
    assert chart["kind"] == "grouped_bar" and len(chart["figure"]["data"]) == 2


def test_unique_label_wins_over_parent_category():
    frame = pd.DataFrame({"channel_group": ["Paid Search", "Paid Search", "Paid Social"],
                          "channel": ["Google Search", "Bing Search", "Meta"], "spend": [3.0, 1.0, 2.0]})
    assert build_chart(frame)["kind"] == "bar"


def test_empty_result_has_no_chart():
    assert build_chart(pd.DataFrame({"x": []})) is None
