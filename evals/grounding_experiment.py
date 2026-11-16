"""Random-claim acceptance: how often does a number checker accept a number that was never in the result?

On an 8-row return-rate table, generate 2,000 random numbers per type and count how many each checker accepts:
the old regex check (kept here only for this comparison) and the structured-claim verifier the app uses.
tests/test_grounding.py reuses the table and claim generators. No LLM calls. Writes evals/reports/grounding_<date>.md.
"""
from __future__ import annotations

import random
import re
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from pantree_insights.grounding import Claim, KeyValue, verify

ROOT = Path(__file__).resolve().parents[1]
N = 2000

CATS = ["Topwear", "Bottomwear", "Shoes", "Sandal", "Bags", "Watches", "Innerwear", "Flip Flops"]


def frame(seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    return pd.DataFrame({"sub_category": CATS, "return_rate": rng.uniform(0.05, 0.30, 8).round(4),
                         "delivered_lines": rng.integers(800, 15000, 8)})


def random_claims(kind: str, n: int = 2000, seed: int = 11) -> list[Claim]:
    rnd = random.Random(seed)
    out = []
    for i in range(n):
        row = [KeyValue(column="sub_category", value=rnd.choice(CATS))]
        if kind == "pct":
            out.append(Claim(id=f"c{i}", value=rnd.uniform(0.01, 0.60), unit="ratio", column="return_rate", row=row))
        elif kind == "ratio":
            other = [KeyValue(column="sub_category", value=rnd.choice(CATS))]
            out.append(Claim(id=f"c{i}", value=rnd.uniform(1.0, 5.0), unit="ratio", column="return_rate", row=row,
                             op="ratio", against=other))
        else:
            out.append(Claim(id=f"c{i}", value=float(rnd.randint(100, 20000)), unit="count",
                             column="delivered_lines", row=row))
    return out


# ----------------------------------------------------------------------------- the old regex check (replaced)
NUMBER = re.compile(r"(?<![\w.])[-+]?(?:₹|rs\.?\s*)?\d[\d,]*(?:\.\d+)?\s*(?:%|[kKmM]\b|lakh|crore|cr\b|l\b)?",
                    re.I)
SCALE = {"k": 1e3, "m": 1e6, "lakh": 1e5, "l": 1e5, "crore": 1e7, "cr": 1e7}


def _parse(token: str) -> tuple[float, bool] | None:
    """'₹19.2L' -> (1920000.0, False); '72%' -> (72.0, True)."""
    t = token.strip().lower().replace("₹", "").replace("rs.", "").replace("rs", "").replace(",", "").strip()
    pct = t.endswith("%")
    t = t.rstrip("%").strip()
    m = re.match(r"([-+]?\d+(?:\.\d+)?)\s*([a-z]*)$", t)
    if not m:
        return None
    value = float(m.group(1)) * SCALE.get(m.group(2), 1)
    return value, pct


MONTHS = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
DATES = re.compile(rf"\b\d{{4}}-\d{{2}}-\d{{2}}\b|\b{MONTHS}\s+\d{{4}}\b"
                   rf"|\b{MONTHS}\s+\d{{1,2}}(?!\d)(?:\s*[-–]\s*\d{{1,2}}(?!\d))?(?:,?\s+\d{{4}})?"
                   rf"|\b\d{{1,2}}(?:st|nd|rd|th)?(?:\s*[-–]\s*\d{{1,2}}(?:st|nd|rd|th)?)?\s+(?:of\s+)?{MONTHS}(?:\s+\d{{4}})?"
                   rf"|\b\d{{1,2}}(?:st|nd|rd|th)\b|\bq[1-4]\b", re.I)


def unsupported_numbers(text: str, frame: pd.DataFrame, rel_tol: float = 0.015) -> list[str]:
    """Numbers in an answer that cannot be traced to the result table (directly or as a simple derivation).

    Allowed: values in the table (any rounding / % form), differences and ratios of two values
    (growth %, shares), years, small counts (<= 12) and ranks.
    """
    values = [float(v) for v in frame.select_dtypes("number").to_numpy().ravel() if pd.notna(v)]
    # Year-like and small integers are context ("top 5", "2026"), not claims.
    derived = set()
    for a in values[:200]:
        for b in values[:200]:
            if b:
                derived.add(a / b)
                derived.add((a - b) / b)
    candidates = values + list(derived)

    def matches(x: float) -> bool:
        return any(abs(x - c) <= rel_tol * max(abs(c), 1e-9) or abs(x - c) < 0.051 for c in candidates)

    bad = []
    text = DATES.sub(" ", text)                  # dates ("October 15-21", "2026-11-03") are not claims
    for token in NUMBER.findall(text):
        parsed = _parse(token)
        if parsed is None:
            continue
        value, pct = parsed
        if not pct and float(value).is_integer() and (abs(value) <= 12 or 1990 <= value <= 2100):
            continue
        options = [value / 100, value] if pct else [value]
        if not any(matches(v) for v in options):
            bad.append(token.strip())
    return bad


# ----------------------------------------------------------------------------- experiment


def old_rates() -> dict[str, float]:
    f, rnd = frame(), random.Random(3)
    pct = sum(not unsupported_numbers(f"{rnd.uniform(1, 60):.1f}%", f) for _ in range(N)) / N
    ratio = sum(not unsupported_numbers(f"{rnd.uniform(1, 5):.1f}x", f) for _ in range(N)) / N
    counts = sum(not unsupported_numbers(f"{rnd.randint(100, 20000):,}", f) for _ in range(N)) / N
    return {"percentage 1-60%": pct, "ratio 1-5x": ratio, "count 100-20,000": counts}


def new_rates() -> dict[str, float]:
    f = frame()
    return {name: sum(verify(f, [c], {}).ok for c in random_claims(kind)) / N
            for name, kind in [("percentage 1-60%", "pct"), ("ratio 1-5x", "ratio"), ("count 100-20,000", "count")]}


def main() -> None:
    old, new = old_rates(), new_rates()
    lines = [f"# Grounding: random-claim acceptance {datetime.now():%Y-%m-%d %H:%M}", "",
             f"8-row result table, {N:,} random numbers per type; share accepted (lower is better). No LLM calls.", "",
             "| Number type | Old regex check | Structured claims |", "|---|---|---|"]
    lines += [f"| {k} | {old[k]:.1%} | {new[k]:.2%} |" for k in old]
    text = "\n".join(lines) + "\n"
    out = ROOT / "evals" / "reports" / f"grounding_{datetime.now():%Y-%m-%d_%H%M}.md"
    out.write_text(text)
    print(text, "\nWritten", out.relative_to(ROOT))


if __name__ == "__main__":
    main()
