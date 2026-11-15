"""Random-claim acceptance: how often does a number checker accept a number that was never in the result?

On an 8-row return-rate table (same fixture as tests/test_grounding.py), generate 2,000 random numbers per
type and count how many each checker accepts. No LLM calls. Writes evals/reports/grounding_<date>.md.
"""
from __future__ import annotations

import random
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_grounding import frame, random_claims  # noqa: E402

from pantree_insights.grounding import verify  # noqa: E402
from pantree_insights.guardrails import unsupported_numbers  # noqa: E402

N = 2000


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
