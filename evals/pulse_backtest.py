"""Pulse backtest (free): inject known shocks into the KPI history and measure detection, old vs new method.

    uv run python evals/pulse_backtest.py --trials 150

For each trial (seeded): pick a random week end and a random series, multiply that series' numerator over
the 7-day window by a factor (spike: +/-25%, +/-50%, +/-100%), run detection at that week end with each
method, and record whether the shocked series is flagged (recall) and how many other unexplained flags
appear (false alerts per week; natural anomalies count against both methods equally). Also reports how
often each method flags a rise in total net sales during the simulator's known sale events.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pantree_insights.config import get_settings  # noqa: E402
from pantree_insights.db import connect, manifest  # noqa: E402
from pantree_insights.pulse import daily_kpis, detect, load_definitions  # noqa: E402
from pantree_insights.semantic import load_metrics  # noqa: E402

FACTORS = [1.25, 1.5, 2.0, 0.75, 0.5]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=150)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    settings = get_settings()
    metrics = load_metrics()
    cfg, kpis = load_definitions(settings.memory_dir)
    con = connect()
    history = daily_kpis(con, metrics, kpis)
    events = con.execute("SELECT name, CAST(start_date AS DATE) AS start_date, CAST(end_date AS DATE) AS end_date "
                         "FROM sale_events").df()
    events["start_date"] = pd.to_datetime(events.start_date)
    events["end_date"] = pd.to_datetime(events.end_date)
    last = pd.Timestamp(manifest()["last_day"])
    methods = {"legacy (mean/SD z>=2.5)": replace(cfg, method="legacy"),
               "robust_bh (median/MAD + BH q=0.10)": replace(cfg, method="robust_bh")}
    # only immediately-maturing KPIs, so the injected window is the one tested
    series = [k for k in history.groupby(["kpi", "breakdown", "segment"]).groups
              if kpis[k[0]].get("mature_after_days", 0) == 0]
    rnd = random.Random(args.seed)
    first_end = history.day.min() + timedelta(days=7 * 10)
    results = {m: {"hits": 0, "trials": 0, "false_alerts": 0, "by_factor": {}} for m in methods}
    for _ in range(args.trials):
        kpi, breakdown, segment = rnd.choice(series)
        end = first_end + timedelta(days=rnd.randrange((last - first_end).days))
        factor = rnd.choice(FACTORS)
        h = history.copy()
        mask = ((h.kpi == kpi) & (h.breakdown == breakdown) & (h.segment == segment)
                & (h.day > end - timedelta(days=7)) & (h.day <= end))
        h.loc[mask, "numerator"] *= factor
        target = f"{kpi}|{breakdown}|{segment}"
        for name, method_cfg in methods.items():
            flags = detect(h, metrics, kpis, method_cfg, end.date(), events, "eval")
            hit = any(f.id == target for f in flags)
            r = results[name]
            r["trials"] += 1
            r["hits"] += hit
            r["false_alerts"] += sum(1 for f in flags if f.id != target and not f.explained)
            fb = r["by_factor"].setdefault(str(factor), [0, 0])
            fb[0] += hit
            fb[1] += 1

    event_weeks = [pd.Timestamp(e) for e in events.end_date if first_end <= pd.Timestamp(e) <= last]
    for name, method_cfg in methods.items():
        flagged = 0
        for end in event_weeks:
            flags = detect(history, metrics, kpis, method_cfg, end.date(), events, "eval")
            flagged += any(f.kpi == "net_sales" and f.breakdown == "total" and f.change_pct > 0 for f in flags)
        results[name]["event_weeks"] = [flagged, len(event_weeks)]

    lines = [f"# Pulse backtest {datetime.now():%Y-%m-%d %H:%M} ({settings.data_size} shop)", "",
             f"{args.trials} injected shocks (seed {args.seed}): one series, one 7-day window, numerator x "
             f"{', '.join(map(str, FACTORS))}. Detection at that week end. No LLM calls.", "",
             "| Method | Recall (shock flagged) | False alerts per week (other unexplained flags) | "
             "Precision* | Sale-event weeks with net-sales rise flagged |", "|---|---|---|---|---|"]
    for name, r in results.items():
        recall = r["hits"] / r["trials"]
        fa = r["false_alerts"] / r["trials"]
        precision = r["hits"] / (r["hits"] + r["false_alerts"]) if r["hits"] + r["false_alerts"] else 0
        ew = r["event_weeks"]
        lines.append(f"| {name} | {r['hits']}/{r['trials']} ({recall:.0%}) | {fa:.2f} | {precision:.0%} | "
                     f"{ew[0]}/{ew[1]} |")
    lines += ["", "Recall by shock size:", "", "| Factor | " + " | ".join(results) + " |",
              "|---|" + "---|" * len(results)]
    for factor in map(str, FACTORS):
        cells = [f"{results[m]['by_factor'].get(factor, [0, 0])[0]}/{results[m]['by_factor'].get(factor, [0, 0])[1]}"
                 for m in results]
        lines.append(f"| x{factor} | " + " | ".join(cells) + " |")
    lines += ["", "*Precision treats every other unexplained flag as false; some are real anomalies in the "
              "simulated data, so this is a lower bound for both methods."]
    out = ROOT / "evals" / "reports"
    stamp = f"pulse_backtest_{datetime.now():%Y-%m-%d_%H%M}"
    (out / f"{stamp}.json").write_text(json.dumps(results, indent=1))
    (out / f"{stamp}.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
