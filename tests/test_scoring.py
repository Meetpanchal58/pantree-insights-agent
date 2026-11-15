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
