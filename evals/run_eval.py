"""Score the agent on evals/questions.yaml + the verified examples.

    uv run python evals/run_eval.py --dry-run            # free: checks every gold SQL runs, no LLM
    uv run python evals/run_eval.py --limit 5 --yes      # real run on 5 questions
    uv run python evals/run_eval.py --ids h02,m04 --yes

Execution accuracy: the agent's result matches the gold result when every numeric column of the gold
result has a column in the agent's result with the same sorted values (within 1%) and the same row count.
Column names, column order and extra columns do not matter.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pantree_insights.config import get_settings  # noqa: E402
from pantree_insights.db import connect, run_query  # noqa: E402
from pantree_insights.guardrails import check_sql  # noqa: E402
from pantree_insights.semantic import load_examples  # noqa: E402

COST_PER_QUESTION = 0.03


def load_questions() -> list[dict]:
    items = [{"id": e.id, "question": e.question, "gold_sql": e.sql, "expect": "answer",
              "difficulty": e.difficulty, "tags": e.tags} for e in load_examples().examples]
    items += yaml.safe_load((ROOT / "evals" / "questions.yaml").read_text())["questions"]
    return items


def numeric_columns(frame: pd.DataFrame) -> list[list[float]]:
    cols = []
    for c in frame.columns:
        if pd.api.types.is_numeric_dtype(frame[c]) and frame[c].dtype != bool:
            cols.append(sorted(float(v) for v in frame[c].dropna()))
    return cols


def same(a: list[float], b: list[float], tol: float = 0.01) -> bool:
    return len(a) == len(b) and all(abs(x - y) <= tol * max(abs(x), abs(y), 1e-9) or abs(x - y) < 1e-6
                                    for x, y in zip(a, b, strict=True))


def results_match(gold: pd.DataFrame, agent: pd.DataFrame) -> bool:
    if len(gold) != len(agent):
        return False
    gold_cols, agent_cols = numeric_columns(gold), numeric_columns(agent)
    if not gold_cols:
        return gold.astype(str).values.tolist() == agent.astype(str).values.tolist()
    return all(any(same(g, a) for a in agent_cols) for g in gold_cols)


def run(items: list[dict], dry_run: bool) -> list[dict]:
    con = connect()
    agent = None
    if not dry_run:
        from pantree_insights.graph import Agent
        agent = Agent()
    rows = []
    for item in items:
        row = {k: item.get(k) for k in ("id", "question", "expect", "difficulty")}
        row["tags"] = item.get("tags", [])
        gold = None
        if item.get("gold_sql"):
            gold = run_query(con, check_sql(item["gold_sql"]).sql).frame
        if dry_run:
            row["gold_rows"] = None if gold is None else len(gold)
            rows.append(row)
            continue
        started = time.time()
        _, events = agent.start(item["question"], use_defaults=item["expect"] != "clarify")
        events = list(events)
        final = events[-1]
        row.update(seconds=round(time.time() - started, 1), status=final.get("status") or final["type"],
                   cost=(final.get("cost") or {}).get("usd", 0.0), attempts=final.get("attempts"),
                   sql=final.get("sql"), warnings=final.get("warnings", []))
        if item["expect"] == "clarify":
            row["passed"] = final["type"] == "clarify"
        elif item["expect"] == "decline":
            row["passed"] = final.get("status") == "declined"
        else:
            ok = final.get("status") == "done" and final.get("result") is not None
            if ok and gold is not None:
                res = final["result"]
                ok = results_match(gold, pd.DataFrame(res["data"], columns=res["columns"]))
            row["passed"] = bool(ok)
        rows.append(row)
        print(f"{'PASS' if row['passed'] else 'FAIL'} {row['id']:4s} {row['seconds']:5.1f}s ${row['cost']:.3f} "
              f"{item['question'][:70]}", flush=True)
    return rows


def summary(rows: list[dict]) -> str:
    frame = pd.DataFrame(rows)
    settings = get_settings()
    lines = [f"# Eval {datetime.now():%Y-%m-%d %H:%M} ({settings.data_size} shop, {settings.model_main})", ""]
    total = frame["passed"].mean()
    lines.append(f"**Overall: {frame['passed'].sum()}/{len(frame)} passed ({total:.0%})** · "
                 f"cost ${frame['cost'].sum():.2f} · median {frame['seconds'].median():.0f}s per question\n")
    for col in ("expect", "difficulty"):
        lines.append(f"| {col} | passed | total | rate |\n|---|---|---|---|")
        for key, g in frame.groupby(col):
            lines.append(f"| {key} | {g['passed'].sum()} | {len(g)} | {g['passed'].mean():.0%} |")
        lines.append("")
    failed = frame[~frame["passed"]]
    if len(failed):
        lines.append("Failed: " + ", ".join(f"{r.id} ({r.status})" for r in failed.itertuples()))
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="only check that gold SQL runs (free)")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--ids", help="comma-separated ids")
    parser.add_argument("--yes", action="store_true", help="confirm real LLM calls")
    args = parser.parse_args()
    items = load_questions()
    if args.ids:
        wanted = set(args.ids.split(","))
        items = [i for i in items if i["id"] in wanted]
    if args.limit:
        items = items[: args.limit]
    if args.dry_run:
        rows = run(items, dry_run=True)
        print(f"{len(rows)} questions; gold SQL ran for {sum(r['gold_rows'] is not None for r in rows)}")
        return
    estimate = len(items) * COST_PER_QUESTION
    if not args.yes:
        print(f"{len(items)} questions, about ${estimate:.2f} of OpenAI usage. Re-run with --yes to start.")
        return
    rows = run(items, dry_run=False)
    out = ROOT / "evals" / "reports"
    out.mkdir(parents=True, exist_ok=True)
    stamp = f"{datetime.now():%Y-%m-%d_%H%M}"
    (out / f"{stamp}.json").write_text(json.dumps(rows, indent=1, default=str))
    text = summary(rows)
    (out / f"{stamp}.md").write_text(text + "\n")
    print("\n" + text)


if __name__ == "__main__":
    main()
