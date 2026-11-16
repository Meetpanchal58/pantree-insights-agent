"""Grounding: numbers in answers must be verifiable claims about the result table.

The random-claim experiment: on an 8-row return-rate result, generate 2,000 random claims per type and
count how many a checker accepts. The old regex check (now only in evals/grounding_experiment.py) accepted most of
them; the structured-claim verifier must accept < 1%.
"""
import random
import sys
from pathlib import Path

import pytest

from pantree_insights.grounding import Claim, KeyValue, free_numbers, render, verify

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
from grounding_experiment import frame, random_claims, unsupported_numbers  # noqa: E402


@pytest.mark.parametrize("kind", ["pct", "ratio", "count"])
def test_random_claims_are_rejected(kind):
    f = frame()
    accepted = sum(verify(f, [c], {}).ok for c in random_claims(kind))
    assert accepted / 2000 < 0.01, f"{kind}: {accepted}/2000 random claims accepted"


def test_old_regex_check_accepted_most_random_claims():
    """Documents why the old check was replaced (see README): it accepts most random numbers."""
    f = frame()
    rnd = random.Random(3)
    pct = sum(not unsupported_numbers(f"{rnd.uniform(1, 60):.1f}%", f) for _ in range(2000)) / 2000
    counts = sum(not unsupported_numbers(f"{rnd.randint(100, 20000):,}", f) for _ in range(2000)) / 2000
    assert pct > 0.9 and counts > 0.2          # measured: ~100% and ~47% (8-row frame)


def test_true_claims_verify_for_every_op():
    f = frame()
    shoes, bags = f.set_index("sub_category").loc["Shoes"], f.set_index("sub_category").loc["Bags"]
    s = [KeyValue(column="sub_category", value="Shoes")]
    b = [KeyValue(column="sub_category", value="Bags")]
    claims = [
        Claim(id="c1", value=shoes.return_rate, unit="ratio", column="return_rate", row=s),
        Claim(id="c2", value=shoes.return_rate - bags.return_rate, unit="pct_points", column="return_rate", row=s,
              op="diff", against=b),
        Claim(id="c3", value=shoes.return_rate / bags.return_rate, unit="ratio", column="return_rate", row=s,
              op="ratio", against=b),
        Claim(id="c4", value=shoes.delivered_lines / f.delivered_lines.sum(), unit="ratio", column="delivered_lines",
              row=s, op="share"),
        Claim(id="c5", value=float(f.delivered_lines.sum()), unit="count", column="delivered_lines", row=[], op="sum"),
        Claim(id="c6", value=shoes.delivered_lines / bags.delivered_lines - 1, unit="ratio", column="delivered_lines",
              row=s, op="pct_change", against=b),
    ]
    result = verify(f, claims, {"headline": "Shoes return {c1}, {c2} above bags."})
    assert result.ok, result.problems


def test_display_rounding_is_allowed_but_not_more():
    f = frame()
    actual = float(f.set_index("sub_category").loc["Shoes", "return_rate"])
    s = [KeyValue(column="sub_category", value="Shoes")]
    rounded = round(actual * 100, 1) / 100                 # shown as one decimal percent
    assert verify(f, [Claim(id="c1", value=rounded, unit="ratio", column="return_rate", row=s, decimals=1)], {}).ok
    assert not verify(f, [Claim(id="c1", value=actual + 0.01, unit="ratio", column="return_rate", row=s)], {}).ok


def test_free_numbers_in_prose_fail_but_dates_years_and_ranks_pass():
    assert free_numbers("Top 5 brands in 2026, from 3 Nov to 8 Nov; revenue rose {c1}.") == []
    assert free_numbers("Revenue was Rs 25 lakh, up 140%.") == ["25", "140%"]
    f = frame()
    result = verify(f, [], {"findings": ["Returns are 25.3% for shoes."]})
    assert not result.ok and "25.3%" in " ".join(result.problems)


def test_unknown_column_or_row_fails():
    f = frame()
    bad_col = Claim(id="c1", value=0.2, unit="ratio", column="no_such", row=[KeyValue(column="sub_category",
                                                                                      value="Shoes")])
    bad_row = Claim(id="c1", value=0.2, unit="ratio", column="return_rate", row=[KeyValue(column="sub_category",
                                                                                          value="Hats")])
    assert not verify(f, [bad_col], {}).ok and not verify(f, [bad_row], {}).ok


def test_render_formats_claims():
    claims = [Claim(id="c1", value=0.2531, unit="ratio", column="return_rate", row=[], decimals=1),
              Claim(id="c2", value=1919491.0, unit="inr", column="revenue", row=[]),
              Claim(id="c3", value=0.027, unit="pct_points", column="return_rate", row=[]),
              Claim(id="c4", value=1423.0, unit="count", column="orders", row=[])]
    text = render("{c1} returns, Rs {c2}, {c3} higher, {c4} orders", claims)
    assert text == "25.3% returns, Rs 19.19 lakh, 2.7 pp higher, 1,423 orders"


def test_brand_case_is_preserved_when_matching_claim_rows():
    import pandas as pd

    from pantree_insights.grounding import Claim, KeyValue, actual

    data = pd.DataFrame({"brand": ["ADIDAS", "Adidas"], "orders": [10, 20]})
    claim = Claim(id="c1", value=10, unit="count", column="orders", row=[KeyValue(column="brand", value="ADIDAS")])
    assert actual(data, claim) == 10
