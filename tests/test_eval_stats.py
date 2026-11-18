"""Aggregation of repeated runs (evals/stats.py) on a fake rows fixture."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
from stats import fmt, paired_difference, summarize  # noqa: E402


def rows(pattern: dict[str, list[bool]]) -> list[dict]:
    return [{"id": q, "repeat": r, "passed": v} for q, vals in pattern.items() for r, v in enumerate(vals)]


FIXTURE = rows({"q1": [True, True, True], "q2": [True, False, True], "q3": [False, False, False],
                "q4": [True, True, True]})


def test_mean_and_pass_k():
    s = summarize(FIXTURE)
    assert s["n_questions"] == 4 and s["k"] == 3
    assert abs(s["mean"] - 8 / 12) < 1e-9
    assert s["pass_k"] == 0.5                                  # q1 and q4 pass all 3 repeats


def test_ci_is_seeded_and_brackets_the_mean():
    a, b = summarize(FIXTURE), summarize(FIXTURE)
    assert a["ci"] == b["ci"]
    assert a["ci"][0] <= a["mean"] <= a["ci"][1]
    low, high = summarize(rows({f"q{i}": [True] for i in range(10)}))["ci"]
    assert 0.7 < low < 0.8 and high == 1.0


def test_paired_difference_pairs_on_question_and_repeat():
    better = rows({"q1": [True, True, True], "q2": [True, True, True], "q3": [True, False, False],
                   "q4": [True, True, True]})
    d = paired_difference(better, FIXTURE)
    assert d["n_pairs"] == 12 and abs(d["diff"] - 2 / 12) < 1e-9
    assert d["ci"][0] <= d["diff"] <= d["ci"][1]
    assert paired_difference(FIXTURE, FIXTURE)["diff"] == 0


def test_missing_measure_and_format():
    assert summarize([{"id": "q1", "repeat": 0, "passed": None}])["mean"] is None
    assert fmt(summarize(FIXTURE)).startswith("67% [")
    assert "pass^3 50%" in fmt(summarize(FIXTURE))


def test_unequal_repeats_do_not_claim_pass_k():
    result = summarize(rows({"q1": [True], "q2": [True, True]}))
    assert result["k"] == 0 and result["pass_k"] is None
