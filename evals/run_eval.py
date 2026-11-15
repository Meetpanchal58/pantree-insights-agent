"""End-to-end evaluation of the agent (paid: real LLM calls) and free gold-SQL checks.

    uv run python evals/run_eval.py --dry-run                     # free: every gold SQL runs, annotations valid
    uv run python evals/run_eval.py --suite holdout               # shows the cost estimate, then asks for --yes
    uv run python evals/run_eval.py --suite holdout --yes         # paid run on the unseen holdout
    uv run python evals/run_eval.py --suite regression --yes      # paid run on the regression suite
    uv run python evals/run_eval.py --rescore evals/reports/<file>.json   # re-score a saved run (free)

Suites
  regression  semantic/examples.yaml (each question's own example is hidden from retrieval: no leakage)
              + evals/questions.yaml
  holdout     evals/holdout.yaml: unseen questions, never in examples or prompts (tests enforce this)

Scoring (evals/scoring.py): rows aligned on business dimensions, metrics within 1%; missing/extra/duplicate
rows and values on the wrong category fail. Holdout uses annotated key/metric columns; regression infers
them (non-numeric columns = keys, main metric = metric).
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals"))

from scoring import infer_columns, score  # noqa: E402

from pantree_insights.config import get_settings  # noqa: E402
from pantree_insights.db import connect, run_query  # noqa: E402
from pantree_insights.guardrails import check_sql  # noqa: E402
from pantree_insights.semantic import load_examples  # noqa: E402

COST_PER_QUESTION = 0.025      # measured average on the full shop with gpt-6.1-terra (Nov 2026)


def load_suite(suite: str) -> list[dict]:
    items: list[dict] = []
    if suite in ("regression", "all"):
        for e in load_examples().examples:
            items.append({"suite": "regression", "id": e.id, "question": e.question, "gold_sql": e.sql,
                          "expect": "answer", "difficulty": e.difficulty, "exclude": [e.id]})
        for q in yaml.safe_load((ROOT / "evals" / "questions.yaml").read_text())["questions"]:
            items.append({"suite": "regression", "exclude": [], **q})
    if suite in ("holdout", "all"):
        for q in yaml.safe_load((ROOT / "evals" / "holdout.yaml").read_text())["questions"]:
            items.append({"suite": "holdout", "exclude": [], **q})
    return items


def gold_frame(con, item: dict) -> tuple[pd.DataFrame, list[str], list[str]]:
    frame = run_query(con, check_sql(item["gold_sql"]).sql).frame
    if item.get("key_columns") is not None and item.get("metric_columns"):
        return frame, item["key_columns"], item["metric_columns"]
    keys, metrics = infer_columns(frame)
    return frame, keys, metrics


def judge(con, item: dict, final: dict) -> tuple[bool, str]:
    expect = item["expect"]
    if expect == "clarify":
        return final["type"] == "clarify", "asked to clarify" if final["type"] == "clarify" else "did not clarify"
    if expect == "decline":
        ok = final.get("status") == "declined"
        return ok, "declined" if ok else f"not declined ({final.get('status') or final['type']})"
    if final.get("status") != "done" or not final.get("sql"):
        return False, f"no answer ({final.get('status') or final['type']}: {final.get('message') or ''})"[:200]
    gold, keys, metrics = gold_frame(con, item)
    agent = run_query(con, final["sql"]).frame
    return score(gold, agent, keys, metrics)


def run(items: list[dict]) -> list[dict]:
    from pantree_insights.graph import Agent

    con = connect()
    agent = Agent()
    rows = []
    for item in items:
        started = time.time()
        _, events = agent.start(item["question"], use_defaults=item["expect"] != "clarify",
                                exclude_examples=item.get("exclude"))
        final = list(events)[-1]
        warnings = final.get("warnings", [])
        row = {"suite": item["suite"], "id": item["id"], "question": item["question"], "expect": item["expect"],
               "difficulty": item.get("difficulty"), "seconds": round(time.time() - started, 1),
               "status": final.get("status") or final["type"], "cost": (final.get("cost") or {}).get("usd", 0.0),
               "attempts": final.get("attempts") or 0, "sql": final.get("sql"), "warnings": warnings,
               "grounded": final.get("status") == "done" and not any("verify" in w for w in warnings)}
        row["passed"], row["reason"] = judge(con, item, final)
        rows.append(row)
        print(f"{'PASS' if row['passed'] else 'FAIL'} {row['suite'][:4]} {row['id']:4s} {row['seconds']:5.1f}s "
              f"${row['cost']:.3f}  {row['reason'][:60]}  | {item['question'][:60]}", flush=True)
    return rows


def rate(part: list[dict], key: str = "passed") -> str:
    return f"{sum(r[key] for r in part)}/{len(part)} ({sum(r[key] for r in part) / len(part):.0%})" if part else "-"


def summary(rows: list[dict]) -> str:
    s = get_settings()
    out = [f"# End-to-end evaluation {datetime.now():%Y-%m-%d %H:%M}", "",
           f"Data: {s.data_size} shop · model: {s.model_main} · scoring: dimension-aware, 1% tolerance", ""]
    for suite in ("holdout", "regression"):
        part = [r for r in rows if r["suite"] == suite]
        if not part:
            continue
        answer = [r for r in part if r["expect"] == "answer"]
        done = [r for r in answer if r["status"] == "done"]
        retried = [r for r in answer if r["attempts"] > 1]
        out += [f"## {suite.capitalize()} ({len(part)} questions)", "", "| Measure | Result |", "|---|---|",
                f"| SQL execution success (answer questions) | {len(done)}/{len(answer)} "
                f"({len(done) / len(answer):.0%}) |" if answer else "| SQL execution success | - |",
                f"| Dimension-aware result accuracy | {rate(answer)} |",
                f"| Clarification accuracy | {rate([r for r in part if r['expect'] == 'clarify'])} |",
                f"| Decline accuracy | {rate([r for r in part if r['expect'] == 'decline'])} |",
                f"| Grounded-number rate (answers passing the number check) | {rate(done, 'grounded')} |",
                f"| Retry / validator recovery (correct after >1 SQL attempt) | {rate(retried)} |",
                f"| Median latency per question | {statistics.median(r['seconds'] for r in part):.0f} s |",
                f"| Cost per question (mean) | ${statistics.mean(r['cost'] for r in part):.3f} |", ""]
        fails = [r for r in part if not r["passed"]]
        if fails:
            out.append("Failures:")
            out += [f"- {r['id']} \"{r['question']}\": {r['reason']}" for r in fails]
            out.append("")
    return "\n".join(line for line in out if line is not None)


def dry_run(items: list[dict]) -> None:
    con = connect()
    bad = []
    for item in items:
        if item["expect"] != "answer":
            continue
        try:
            frame, keys, metrics = gold_frame(con, item)
            missing = [c for c in keys + metrics if c not in frame.columns]
            if missing or frame.empty or (keys and frame.duplicated(keys).any()):
                bad.append(f"{item['id']}: missing columns {missing} / empty / duplicate keys")
        except Exception as error:                                   # report every broken gold query
            bad.append(f"{item['id']}: {error}")
    answers = sum(i["expect"] == "answer" for i in items)
    print(f"{len(items)} questions ({answers} with gold SQL): {answers - len(bad)} gold queries valid")
    for b in bad:
        print("  BROKEN", b)
    if bad:
        sys.exit(1)


def rescore(report: Path) -> None:
    rows = json.loads(report.read_text())
    by_id = {(i["suite"], i["id"]): i for i in load_suite("all")}
    con = connect()
    for row in rows:
        item = by_id[(row["suite"], row["id"])]
        if item["expect"] == "answer" and row["status"] == "done" and row.get("sql"):
            gold, keys, metrics = gold_frame(con, item)
            row["passed"], row["reason"] = score(gold, run_query(con, row["sql"]).frame, keys, metrics)
    report.write_text(json.dumps(rows, indent=1, default=str))
    text = summary(rows)
    report.with_suffix(".md").write_text(text + "\n")
    print(text)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", choices=["holdout", "regression", "all"], default="holdout")
    parser.add_argument("--dry-run", action="store_true", help="free: check gold SQL and annotations")
    parser.add_argument("--ids", help="comma-separated ids")
    parser.add_argument("--yes", action="store_true", help="confirm paid LLM calls")
    parser.add_argument("--rescore", type=Path, help="re-score a saved report (free)")
    args = parser.parse_args()
    if args.rescore:
        return rescore(args.rescore)
    items = load_suite("all" if args.dry_run else args.suite)
    if args.ids:
        wanted = set(args.ids.split(","))
        items = [i for i in items if i["id"] in wanted]
    if args.dry_run:
        return dry_run(items)
    if not args.yes:
        print(f"{len(items)} questions, about ${len(items) * COST_PER_QUESTION:.2f} of OpenAI usage. "
              "Re-run with --yes to start.")
        return
    rows = run(items)
    out = ROOT / "evals" / "reports"
    out.mkdir(parents=True, exist_ok=True)
    stamp = f"e2e_{args.suite}_{datetime.now():%Y-%m-%d_%H%M}"
    (out / f"{stamp}.json").write_text(json.dumps(rows, indent=1, default=str))
    text = summary(rows)
    (out / f"{stamp}.md").write_text(text + "\n")
    print("\n" + text)


if __name__ == "__main__":
    main()
