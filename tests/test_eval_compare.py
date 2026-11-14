import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
from run_eval import results_match  # noqa: E402


def test_match_ignores_names_order_and_extra_columns():
    gold = pd.DataFrame({"category": ["A", "B"], "rate": [0.25, 0.1]})
    agent = pd.DataFrame({"cat": ["B", "A"], "volume": [9, 8], "return_rate": [0.1001, 0.2502]})
    assert results_match(gold, agent)


def test_mismatch_on_values_or_rows():
    gold = pd.DataFrame({"x": ["A", "B"], "v": [1.0, 2.0]})
    assert not results_match(gold, pd.DataFrame({"x": ["A", "B"], "v": [1.0, 2.5]}))
    assert not results_match(gold, pd.DataFrame({"x": ["A"], "v": [1.0]}))
