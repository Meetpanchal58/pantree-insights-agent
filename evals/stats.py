"""Aggregation for repeated evaluation runs: mean, bootstrap CIs, pass^k and paired differences.

Rows are dicts with at least: id (question), repeat, and a boolean measure (e.g. "passed").
- mean: share of (question, repeat) rows that are True
- 95% CI: bootstrap over questions (each resample draws whole questions with all their repeats),
  2,000 resamples, seeded, percentile interval
- pass^k: share of questions True in all k repeats
- paired difference vs a reference variant: rows paired on (id, repeat); bootstrap over questions
"""
from __future__ import annotations

import math
import random
from collections import defaultdict

RESAMPLES = 2000
SEED = 20261115


def _by_question(rows: list[dict], key: str) -> dict[tuple, list[bool]]:
    out: dict[tuple, list[bool]] = defaultdict(list)
    for r in rows:
        if r.get(key) is not None:
            out[(r.get("suite", ""), r.get("set", ""), r["id"])].append(bool(r[key]))
    return out


def summarize(rows: list[dict], key: str = "passed", resamples: int = RESAMPLES, seed: int = SEED) -> dict:
    """{"n_questions", "k", "mean", "ci": (low, high), "pass_k"} for one boolean measure."""
    groups = _by_question(rows, key)
    if not groups:
        return {"n_questions": 0, "k": 0, "mean": None, "ci": (None, None), "pass_k": None}
    ids = sorted(groups)
    counts = {len(v) for v in groups.values()}
    k = counts.pop() if len(counts) == 1 else 0
    totals = [(sum(groups[i]), len(groups[i])) for i in ids]
    mean = sum(t for t, _ in totals) / sum(n for _, n in totals)
    rnd = random.Random(seed)
    stats = []
    for _ in range(resamples):
        sample = [totals[rnd.randrange(len(totals))] for _ in totals]
        stats.append(sum(t for t, _ in sample) / sum(n for _, n in sample))
    stats.sort()
    low, high = stats[int(0.025 * resamples)], stats[int(0.975 * resamples) - 1]
    if mean in (0.0, 1.0):
        # Bootstrap degenerates at the boundary. Use a Wilson interval over questions,
        # not repeats, so a small all-pass sample does not imply certain accuracy.
        z, n = 1.959963984540054, len(ids)
        center = (mean + z * z / (2 * n)) / (1 + z * z / n)
        width = z * math.sqrt(mean * (1 - mean) / n + z * z / (4 * n * n)) / (1 + z * z / n)
        low, high = min(mean, max(0.0, center - width)), max(mean, min(1.0, center + width))
    pass_k = sum(all(groups[i]) for i in ids) / len(ids) if k else None
    return {"n_questions": len(ids), "k": k, "mean": mean, "ci": (low, high), "pass_k": pass_k}


def paired_difference(rows: list[dict], reference: list[dict], key: str = "passed",
                      resamples: int = RESAMPLES, seed: int = SEED) -> dict:
    """Mean difference (rows - reference) with a paired bootstrap CI over questions.
    Only (id, repeat) pairs present in both are used."""
    ref = {(r["id"], r["repeat"]): bool(r[key]) for r in reference if r.get(key) is not None}
    diffs: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        pair = (r["id"], r["repeat"])
        if r.get(key) is not None and pair in ref:
            diffs[r["id"]].append(int(bool(r[key])) - int(ref[pair]))
    if not diffs:
        return {"n_pairs": 0, "diff": None, "ci": (None, None)}
    ids = sorted(diffs)
    totals = [(sum(diffs[i]), len(diffs[i])) for i in ids]
    diff = sum(t for t, _ in totals) / sum(n for _, n in totals)
    rnd = random.Random(seed)
    stats = []
    for _ in range(resamples):
        sample = [totals[rnd.randrange(len(totals))] for _ in totals]
        stats.append(sum(t for t, _ in sample) / sum(n for _, n in sample))
    stats.sort()
    return {"n_pairs": sum(n for _, n in totals), "diff": diff,
            "ci": (stats[int(0.025 * resamples)], stats[int(0.975 * resamples) - 1])}


def fmt(summary: dict) -> str:
    """'84% [77-90%] (n=30, k=5, pass^5 73%)' or '-'."""
    if summary.get("mean") is None:
        return "-"
    low, high = summary["ci"]
    text = f"{summary['mean']:.0%} [{low:.0%}-{high:.0%}] (n={summary['n_questions']}"
    if summary["k"] > 1:
        text += f", k={summary['k']}, pass^{summary['k']} {summary['pass_k']:.0%}"
    return text + ")"
