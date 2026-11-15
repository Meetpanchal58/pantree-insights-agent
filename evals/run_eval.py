"""End-to-end evaluation of the agent (paid: real LLM calls) and free checks.

    uv run python evals/run_eval.py --dry-run                                   # free: gold SQL + suite files
    uv run python evals/run_eval.py --suite holdout                             # prints the cost estimate
    uv run python evals/run_eval.py --suite holdout --repeats 5 --yes           # paid
    uv run python evals/run_eval.py --suite holdout --variant baseline --repeats 3 --yes
    uv run python evals/run_eval.py --rescore evals/reports/<file>.json          # free re-score of a saved run

Suites
  regression  semantic/examples.yaml (each question's own example hidden from retrieval) + evals/questions.yaml
  holdout     evals/holdout.yaml + evals/holdout_external.yaml + evals/behaviour.yaml, plus an over-asking
              pass: every clear answer question run once more without defaults (set "overask")

Per row: result_correct (dimension-aware table match, evals/scoring.py), answer_correct (result correct, the
answer's claims verified in the app AND re-verified against the gold result), decline_layer (input_guard or
planner), failure_type. Reports give mean, 95% bootstrap CI over questions and pass^k (evals/stats.py).
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals"))

from scoring import align, infer_columns  # noqa: E402
from stats import fmt, summarize  # noqa: E402

from pantree_insights.config import get_settings  # noqa: E402
from pantree_insights.db import connect, run_query  # noqa: E402
from pantree_insights.grounding import Claim, KeyValue, verify  # noqa: E402
from pantree_insights.guardrails import check_sql  # noqa: E402
from pantree_insights.semantic import load_examples  # noqa: E402

COST_PER_QUESTION = 0.013      # measured mean on the full-shop holdout with gpt-6.1-terra (Nov 15 2026)
NO_DEFAULTS = {"clarify", "overask"}


def _yaml(name: str) -> list[dict]:
    return yaml.safe_load((ROOT / "evals" / name).read_text())["questions"] or []


def load_suite(suite: str) -> list[dict]:
    items: list[dict] = []
    if suite in ("regression", "all"):
        for e in load_examples().examples:
            items.append({"suite": "regression", "set": "answer", "id": e.id, "question": e.question,
                          "gold_sql": e.sql, "expect": "answer", "difficulty": e.difficulty, "exclude": [e.id]})
        for q in _yaml("questions.yaml"):
            items.append({"suite": "regression", "set": q["expect"], "exclude": [], **q})
    if suite in ("holdout", "all"):
        annotated = _yaml("holdout.yaml") + _yaml("holdout_external.yaml")
        for q in annotated:
            items.append({"suite": "holdout", "set": q["expect"], "exclude": [], **q})
        for q in _yaml("behaviour.yaml"):
            items.append({"suite": "holdout", "exclude": [], **q})
        for q in annotated:                              # over-asking pass on clear questions
            if q["expect"] == "answer":
                items.append({"suite": "holdout", "set": "overask", "exclude": [], **{**q, "id": f"o-{q['id']}"}})
    for item in items:
        item.setdefault("author", "self")
    return items


def gold_frame(con, item: dict) -> tuple[pd.DataFrame, list[str], list[str]]:
    frame = run_query(con, check_sql(item["gold_sql"]).sql).frame
    if item.get("key_columns") is not None and item.get("metric_columns"):
        return frame, item["key_columns"], item["metric_columns"]
    keys, metrics = infer_columns(frame)
    return frame, keys, metrics


def claims_hold_on_gold(claims: list[dict], gold: pd.DataFrame, mapping: dict[str, str]) -> tuple[bool, str]:
    """Re-verify the answer's claims against the GOLD result, translating agent columns to gold columns.
    Claims about columns the gold result does not have (e.g. an extra volume column) cannot be checked
    against gold; they were already verified against the agent's own (correct) result in the app."""
    translated = []
    for c in claims:
        claim = Claim.model_validate(c)
        if claim.column not in mapping or any(kv.column not in mapping for kv in claim.row + (claim.against or [])):
            continue
        tr = lambda kvs: [KeyValue(column=mapping[kv.column], value=kv.value) for kv in kvs]   # noqa: E731
        translated.append(claim.model_copy(update={"column": mapping[claim.column], "row": tr(claim.row),
                                                   "against": tr(claim.against) if claim.against else None}))
    result = verify(gold, translated, {})
    return result.ok, "; ".join(result.problems[:3])


def decline_layer(events: list[dict]) -> str | None:
    for e in events:
        if e.get("step") == "check_input" and e.get("status") == "blocked":
            return "input_guard"
        if e.get("step") == "plan" and (e.get("detail") or {}).get("decision") == "decline":
            return "planner"
    return None


def judge(con, item: dict, final: dict, events: list[dict]) -> dict:
    out = {"result_correct": None, "answer_correct": None, "decline_layer": decline_layer(events)}
    kind, status = item["set"], final.get("status") or final["type"]
    asked = final["type"] == "clarify"
    if kind == "clarify":
        return {**out, "passed": asked, "reason": "asked" if asked else "did not clarify",
                "failure_type": None if asked else "did_not_clarify"}
    if kind in ("decline", "adversarial"):
        ok = status == "declined"
        reason = f"declined by {out['decline_layer']}" if ok else f"not declined ({status})"
        return {**out, "passed": ok, "reason": reason, "failure_type": None if ok else "not_declined"}
    if asked:
        return {**out, "passed": False, "reason": "asked a clarifying question", "failure_type": "over_asked"}
    if status == "declined":
        return {**out, "passed": False, "reason": f"declined by {out['decline_layer']}",
                "failure_type": "declined_wrongly"}
    if kind in ("overask", "false_decline"):
        return {**out, "passed": True, "reason": "answered", "failure_type": None}
    if status != "done" or not final.get("sql"):
        message = final.get("message") or ""
        kind = "sql_error" if "Database error" in message or "attempts" in message else "no_sql"
        return {**out, "passed": False, "reason": f"no answer ({status}: {message})"[:200], "failure_type": kind}
    gold, keys, metrics = gold_frame(con, item)
    agent = run_query(con, final["sql"]).frame
    ok, reason, mapping = align(gold, agent, keys, metrics)
    out["result_correct"] = ok
    if not ok:
        failure = ("missing_rows" if reason.startswith("missing rows") else "extra_rows" if reason.startswith("extra")
                   else "wrong_dimension" if reason.startswith("wrong dimension") else "wrong_metric_value")
        return {**out, "passed": False, "answer_correct": False, "reason": reason, "failure_type": failure}
    claims = (final.get("analysis") or {}).get("claims") or []
    gold_ok, problems = claims_hold_on_gold(claims, gold, mapping)
    out["answer_correct"] = bool(final.get("grounded")) and gold_ok
    reason = "match" if out["answer_correct"] else f"claims: {problems or 'not grounded'}"
    return {**out, "passed": True, "reason": reason,
            "failure_type": None if out["answer_correct"] else "claims_unverified"}


def run(items: list[dict], variant: str, repeats: int, offset: int = 0) -> list[dict]:
    from pantree_insights.graph import VARIANTS, Agent

    settings = get_settings()
    con = connect()
    agent = Agent(features=VARIANTS[variant])
    rows = []
    for repeat in range(offset, offset + repeats):
        for item in items:
            started = time.time()
            _, stream = agent.start(item["question"], use_defaults=item["set"] not in NO_DEFAULTS,
                                    exclude_examples=item.get("exclude"))
            events = list(stream)
            final = events[-1]
            if "LLM budget" in (final.get("message") or ""):      # budget reached: stop, never score it
                print(f"STOP: {final['message']} ({len(rows)} rows kept)", flush=True)
                return rows
            writes = [e for e in events if e.get("step") == "write_sql" and e.get("status") == "done"]
            row = {"suite": item["suite"], "variant": variant, "repeat": repeat, "model": settings.model_main,
                   "temperature": settings.temperature, "id": item["id"], "author": item["author"],
                   "set": item["set"], "expect": item["expect"], "difficulty": item.get("difficulty"),
                   "tuned": bool(item.get("tuned_on")), "question": item["question"],
                   "seconds": round(time.time() - started, 1), "status": final.get("status") or final["type"],
                   "cost": (final.get("cost") or {}).get("usd", 0.0), "attempts": final.get("attempts") or 0,
                   "sql": final.get("sql"), "warnings": final.get("warnings", []),
                   "grounded": final.get("grounded"), "claims": (final.get("analysis") or {}).get("claims"),
                   "context_tables": (writes[0].get("detail") or {}).get("context_tables") if writes else None,
                   "required_tables": item.get("required_tables")}
            row.update(judge(con, item, final, events))
            rows.append(row)
            print(f"{'PASS' if row['passed'] else 'FAIL'} r{repeat} {row['set'][:8]:8s} {row['id']:7s} "
                  f"{row['seconds']:5.1f}s ${row['cost']:.3f}  {row['reason'][:55]}", flush=True)
    return rows


def _rate(rows: list[dict], key: str = "passed") -> str:
    return fmt(summarize(rows, key))


def summary(rows: list[dict]) -> str:
    settings = get_settings()
    variants = sorted({r["variant"] for r in rows})
    temp = rows[0].get("temperature") if rows else None
    out = [f"# End-to-end evaluation {datetime.now():%Y-%m-%d %H:%M}", "",
           f"Data: {settings.data_size} shop · model: {rows[0]['model'] if rows else settings.model_main} · "
           f"temperature: {temp if temp is not None else 'provider default (not settable for this model)'} · "
           f"variant(s): {', '.join(variants)} · repeats: {max(r['repeat'] for r in rows) + 1 if rows else 0}",
           "", "Format: mean [95% bootstrap CI over questions] (n questions, k repeats, pass^k = correct in all k).",
           ""]
    for suite in ("holdout", "regression"):
        part = [r for r in rows if r["suite"] == suite]
        if not part:
            continue
        answer = [r for r in part if r["set"] == "answer"]
        done = [r for r in answer if r["status"] == "done"]
        lines = [
            ("Correct result tables (dimension-aware, 1%)", _rate(answer, "result_correct")),
            ("Correct answers (result + claims verified against gold)", _rate(answer, "answer_correct")),
            ("Correct result tables, excluding questions used for tuning",
             _rate([r for r in answer if not r["tuned"]], "result_correct")),
            ("SQL execution success", _rate([{**r, "ok": r["status"] == "done"} for r in answer], "ok")),
            ("Grounded answers (all claims verified in the app)",
             _rate([{**r, "g": bool(r["grounded"])} for r in done], "g")),
            ("Clarification accuracy (vague questions)", _rate([r for r in part if r["set"] == "clarify"])),
            ("Decline accuracy (unsafe requests)", _rate([r for r in part if r["set"] == "decline"])),
            ("Adversarial declines (paraphrased injection / PII)",
             _rate([r for r in part if r["set"] == "adversarial"])),
            ("False-decline rate (valid questions declined)",
             _rate([{**r, "fd": r["failure_type"] == "declined_wrongly"} for r in part if r["set"] == "false_decline"],
                   "fd")),
            ("Unnecessary clarification rate (clear questions, no defaults)",
             _rate([{**r, "oa": r["failure_type"] == "over_asked"} for r in part if r["set"] == "overask"], "oa")),
            ("Retry / validator recovery (correct after >1 attempt)",
             _rate([r for r in answer if r["attempts"] > 1], "result_correct")),
        ]
        out += [f"## {suite.capitalize()}", "", "| Measure | Result |", "|---|---|"]
        out += [f"| {name} | {value} |" for name, value in lines]
        declines = [r for r in part if r["set"] in ("decline", "adversarial") and r["passed"]]
        layers = Counter(r["decline_layer"] for r in declines)
        out.append(f"| Declines caught by input guard / planner | {layers.get('input_guard', 0)} / "
                   f"{layers.get('planner', 0)} |")
        out.append(f"| Median latency · mean cost per question | "
                   f"{statistics.median(r['seconds'] for r in part):.0f} s · "
                   f"${statistics.mean(r['cost'] for r in part):.3f} |")
        authors = sorted({r["author"] for r in answer})
        if len(authors) > 1:
            out += ["", "| Author | Correct result tables |", "|---|---|"]
            out += [f"| {a} | {_rate([r for r in answer if r['author'] == a], 'result_correct')} |" for a in authors]
        failures = Counter(r["failure_type"] for r in part if r["failure_type"])
        if failures:
            out += ["", "Failure types: " + ", ".join(f"{k} {v}" for k, v in failures.most_common()), "",
                    "Failures:"]
            out += [f"- r{r['repeat']} {r['id']} [{r['failure_type']}] \"{r['question'][:70]}\": {r['reason'][:120]}"
                    for r in part if r["failure_type"]]
        out.append("")
    return "\n".join(out)


def dry_run(items: list[dict]) -> None:
    con = connect()
    bad = []
    for item in items:
        if not item.get("gold_sql"):
            continue
        try:
            frame, keys, metrics = gold_frame(con, item)
            missing = [c for c in keys + metrics if c not in frame.columns]
            if missing or (keys and frame.duplicated(keys).any()):
                bad.append(f"{item['id']}: missing columns {missing} / duplicate keys")
        except Exception as error:                                   # report every broken gold query
            bad.append(f"{item['id']}: {error}")
    sets = Counter(i["set"] for i in items)
    gold = sum(bool(i.get("gold_sql")) for i in items)
    print(f"{len(items)} items {dict(sets)}; {gold - len(bad)}/{gold} gold queries valid")
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
        if item["set"] == "answer" and row["status"] == "done" and row.get("sql"):
            final = {"status": "done", "type": "final", "sql": row["sql"], "grounded": row.get("grounded"),
                     "analysis": {"claims": row.get("claims") or []}}
            row.update(judge(con, item, final, []))
    report.write_text(json.dumps(rows, indent=1, default=str))
    text = summary(rows)
    report.with_suffix(".md").write_text(text + "\n")
    print(text)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", choices=["holdout", "regression", "all"], default="holdout")
    parser.add_argument("--variant", default="full", help="full, full_no_memory, validator, planner, retrieval, "
                                                          "semantic or baseline")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--repeat-offset", type=int, default=0, help="number the repeats from here (combine runs)")
    parser.add_argument("--sets", help="only these sets, e.g. answer,overask")
    parser.add_argument("--dry-run", action="store_true", help="free: check gold SQL and suite files")
    parser.add_argument("--ids", help="comma-separated ids")
    parser.add_argument("--yes", action="store_true", help="confirm paid LLM calls")
    parser.add_argument("--rescore", type=Path, help="re-score a saved report (free)")
    args = parser.parse_args()
    if args.rescore:
        return rescore(args.rescore)
    items = load_suite("all" if args.dry_run else args.suite)
    if args.sets:
        items = [i for i in items if i["set"] in set(args.sets.split(","))]
    if args.ids:
        items = [i for i in items if i["id"] in set(args.ids.split(","))]
    if args.dry_run:
        return dry_run(items)
    calls = len(items) * args.repeats
    if not args.yes:
        print(f"{len(items)} questions x {args.repeats} repeat(s) = {calls} runs, variant {args.variant}: about "
              f"${calls * COST_PER_QUESTION:.2f} of OpenAI usage. Re-run with --yes to start.")
        return
    rows = run(items, args.variant, args.repeats, args.repeat_offset)
    out = ROOT / "evals" / "reports"
    out.mkdir(parents=True, exist_ok=True)
    stamp = (f"e2e_{args.suite}_{args.variant}_r{args.repeat_offset}-{args.repeat_offset + args.repeats - 1}_"
             f"{datetime.now():%Y-%m-%d_%H%M}")
    (out / f"{stamp}.json").write_text(json.dumps(rows, indent=1, default=str))
    text = summary(rows)
    (out / f"{stamp}.md").write_text(text + "\n")
    print("\n" + text)


if __name__ == "__main__":
    main()
