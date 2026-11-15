"""Dimension-aware scoring rules (evals/scoring.py)."""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
from scoring import score  # noqa: E402

GOLD = pd.DataFrame({"category": ["Apparel", "Footwear"], "return_rate": [0.231, 0.253]})


def test_same_answer_with_other_names_order_and_extra_columns():
    agent = pd.DataFrame({"cat": ["footwear", "Apparel"], "delivered": [9, 8], "rate": [0.2531, 0.2309]})
    assert score(GOLD, agent, ["category"], ["return_rate"]) == (True, "match")


def test_percent_scale_accepted():
    agent = pd.DataFrame({"category": ["Apparel", "Footwear"], "return_pct": [23.1, 25.3]})
    assert score(GOLD, agent, ["category"], ["return_rate"])[0]


def test_values_on_the_wrong_category_fail():
    agent = pd.DataFrame({"category": ["Apparel", "Footwear"], "return_rate": [0.253, 0.231]})
    ok, reason = score(GOLD, agent, ["category"], ["return_rate"])
    assert not ok and "metric" in reason


def test_missing_extra_and_duplicate_rows_fail():
    extra = pd.DataFrame({"category": ["Apparel", "Footwear", "Accessories"], "return_rate": [0.231, 0.253, 0.07]})
    missing = GOLD.iloc[:1]
    dup = pd.DataFrame({"category": ["Apparel", "Apparel", "Footwear"], "return_rate": [0.231, 0.231, 0.253]})
    assert not score(GOLD, extra, ["category"], ["return_rate"])[0]
    assert not score(GOLD, missing, ["category"], ["return_rate"])[0]
    assert not score(GOLD, dup, ["category"], ["return_rate"])[0]


def test_tolerance_is_one_percent():
    near = pd.DataFrame({"category": ["Apparel", "Footwear"], "return_rate": [0.2321, 0.2541]})   # <1%
    far = pd.DataFrame({"category": ["Apparel", "Footwear"], "return_rate": [0.240, 0.253]})      # 3.9%
    assert score(GOLD, near, ["category"], ["return_rate"])[0]
    assert not score(GOLD, far, ["category"], ["return_rate"])[0]


def test_dates_align_across_formats_and_single_values():
    gold = pd.DataFrame({"month": pd.to_datetime(["2026-01-01", "2026-02-01"]), "orders": [10, 12]})
    agent = pd.DataFrame({"month": ["2026-01-01T00:00:00.000", "2026-02-01T00:00:00.000"], "orders": [10, 12]})
    assert score(gold, agent, ["month"], ["orders"])[0]
    assert score(pd.DataFrame({"aov": [1796.9]}), pd.DataFrame({"x": [1797.0]}), [], ["aov"])[0]


def test_empty_gold_needs_empty_agent_and_zero_counts_compare():
    empty = pd.DataFrame({"category": [], "orders": []})
    assert score(empty, empty.copy(), ["category"], ["orders"])[0]
    assert not score(empty, pd.DataFrame({"category": ["A"], "orders": [1]}), ["category"], ["orders"])[0]
    assert score(pd.DataFrame({"orders": [0]}), pd.DataFrame({"n": [0]}), [], ["orders"])[0]


def test_descriptive_key_column_is_optional_when_the_other_keys_identify_rows():
    """category is determined by sku_id; leaving it out is the same answer."""
    gold = pd.DataFrame({"sku_id": ["S1", "S2"], "category": ["Footwear", "Apparel"], "qty": [3, 5]})
    agent = pd.DataFrame({"sku_id": ["S2", "S1"], "qty": [5, 3]})
    assert score(gold, agent, ["sku_id", "category"], ["qty"]) == (True, "match")


def test_a_key_column_that_splits_rows_is_still_required():
    gold = pd.DataFrame({"channel_group": ["Paid", "Paid"], "channel": ["Meta", "Google"], "spend": [1.0, 2.0]})
    agent = pd.DataFrame({"channel_group": ["Paid"], "spend": [3.0]})
    assert not score(gold, agent, ["channel_group", "channel"], ["spend"])[0]


def test_computed_labels_match_on_their_words():
    gold = pd.DataFrame({"day_type": ["Normal day", "Sale event"], "discount": [0.1, 0.3]})
    agent = pd.DataFrame({"day_type": ["normal day", "sale-event day"], "discount": [0.1, 0.3]})
    assert score(gold, agent, ["day_type"], ["discount"]) == (True, "match")
    wrong = pd.DataFrame({"day_type": ["normal day", "sale-event day"], "discount": [0.3, 0.1]})
    assert not score(gold, wrong, ["day_type"], ["discount"])[0]


def test_unrequested_top_n_size_compares_the_top_rows():
    from scoring import trim_top_n
    gold = pd.DataFrame({"city": ["A", "B"], "rate": [0.5, 0.4]})
    agent = pd.DataFrame({"city": ["A", "B", "C"], "rate": [0.5, 0.4, 0.3]})
    ranked = "SELECT city, rate FROM t ORDER BY rate DESC LIMIT 2"
    assert score(gold, trim_top_n(agent, gold, ranked, "Which cities have the highest rate?"), ["city"], ["rate"])[0]
    # the question names a size, or the gold is not a ranked LIMIT: no trimming
    assert len(trim_top_n(agent, gold, ranked, "What are the top 2 cities by rate?")) == 3
    assert len(trim_top_n(agent, gold, "SELECT city, rate FROM t", "Rate by city?")) == 3
    # a different top-2 still fails
    other = pd.DataFrame({"city": ["A", "C", "B"], "rate": [0.5, 0.45, 0.4]})
    trimmed = trim_top_n(other, gold, ranked, "Which cities have the highest rate?")
    assert not score(gold, trimmed, ["city"], ["rate"])[0]
