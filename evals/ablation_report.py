"""Ablation table from saved end-to-end reports (free).

    uv run python evals/ablation_report.py evals/reports/e2e_holdout_*.json

Per variant (holdout "answer" questions): correct result tables (mean, 95% CI, pass^k), correct answers,
mean cost and median latency per question, and the paired-bootstrap 95% CI of the difference versus
`full` (paired on question id and repeat).
"""
from __future__ import annotations

import json
import statistics
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from stats import fmt, paired_difference, summarize  # noqa: E402

ORDER = ["baseline", "semantic", "retrieval", "planner", "validator", "full", "full_no_memory"]


def build(paths: list[Path]) -> str:
    rows = [r for p in paths for r in json.loads(p.read_text()) if r["set"] == "answer" and r["suite"] == "holdout"]
    by_variant = {v: [r for r in rows if r["variant"] == v] for v in ORDER if any(r["variant"] == v for r in rows)}
    full = by_variant.get("full", [])
    lines = [f"# Ablation {datetime.now():%Y-%m-%d %H:%M}", "",
             f"Holdout answer questions; sources: {', '.join(p.name for p in paths)}", "",
             "| Variant | Correct result tables | Correct answers | Cost / question | Median latency | "
             "Difference vs full (paired 95% CI) |", "|---|---|---|---|---|---|"]
    for variant, part in by_variant.items():
        diff = "-" if variant == "full" or not full else (
            lambda d: f"{d['diff']:+.0%} [{d['ci'][0]:+.0%}, {d['ci'][1]:+.0%}] (pairs={d['n_pairs']})"
            if d["diff"] is not None else "-")(paired_difference(part, full, "result_correct"))
        lines.append(f"| {variant} | {fmt(summarize(part, 'result_correct'))} | "
                     f"{fmt(summarize(part, 'answer_correct'))} | ${statistics.mean(r['cost'] for r in part):.3f} | "
                     f"{statistics.median(r['seconds'] for r in part):.0f} s | {diff} |")
    missing = [v for v in ORDER if v not in by_variant]
    if missing:
        lines += ["", f"Not run: {', '.join(missing)}."]
    return "\n".join(lines) + "\n"


def main() -> None:
    paths = [Path(p) for p in sys.argv[1:]]
    if not paths:
        sys.exit("usage: ablation_report.py <e2e report json> ...")
    text = build(paths)
    out = Path(__file__).resolve().parent / "reports" / f"ablation_{datetime.now():%Y-%m-%d_%H%M}.md"
    out.write_text(text)
    print(text)


if __name__ == "__main__":
    main()
