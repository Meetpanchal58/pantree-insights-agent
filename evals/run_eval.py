"""End-to-end evaluation (paid: real model calls) with a simulated stakeholder, plus free checks.

    uv run python evals/run_eval.py --suite all --dry-run          # free: gold SQL and suite files
    uv run python evals/run_eval.py --suite all                    # prints the cost estimate
    uv run python evals/run_eval.py --suite all --yes --max-cost-usd 4
    uv run python evals/run_eval.py --rescore evals/reports/<file>.json

Suites: regression (evals/regression.yaml + questions.yaml), holdout (holdout.yaml + behaviour.yaml + an
over-asking pass), coverage (coverage.yaml), flow (flow_regression.yaml; follow-ups with chat history).

Answer questions are run the way a real user works: every clarifying question and the read-back go to a
simulated stakeholder who knows the question's intent (its gold spec, evals/gold_specs.yaml). It confirms a
read-back only if it computes the intended thing, otherwise it says what it wants instead; it never helps beyond
what a user would say. The stakeholder exists only here; in production the user plays this role.

Measures (docs/ARCHITECTURE.md): verified answers equal to the gold (gate: 100%), silent wrong answers (gate: 0),
first reading correct, questions per answer, and the behaviour sets (declines, vague questions, over-asking).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd
import sqlglot
import yaml
from sqlglot import exp

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals"))

from scoring import align, infer_columns, trim_top_n  # noqa: E402
from stats import fmt, summarize  # noqa: E402

from pantree_insights.config import get_settings  # noqa: E402
from pantree_insights.db import connect, manifest, run_query  # noqa: E402
from pantree_insights.guardrails import check_sql  # noqa: E402

COST_PER_QUESTION = 0.012      # measured on the Nov 18 v2 run (prompt-cached interpreter + analyst)
FULL_LIST = re.compile(r"\b(each|every|per)\b", re.I)
MAX_TURNS = 5                  # stakeholder answers per question before giving up


def _yaml(name: str, key: str = "questions") -> list[dict]:
    return yaml.safe_load((ROOT / "evals" / name).read_text())[key] or []


def load_suite(suite: str) -> list[dict]:
    items: list[dict] = []
    if suite in ("regression", "all"):
        for e in _yaml("regression.yaml", "examples"):
            items.append({"suite": "regression", "set": "answer", "expect": "answer", "id": e["id"],
                          "question": e["question"], "gold_sql": e["sql"], "difficulty": e.get("difficulty")})
        for q in _yaml("questions.yaml"):
            items.append({"suite": "regression", "set": q["expect"], **q})
    if suite in ("holdout", "all"):
        for q in _yaml("holdout.yaml"):
            items.append({"suite": "holdout", "set": q["expect"], **q})
        items += [{"suite": "holdout", **q} for q in _yaml("behaviour.yaml")]
        for q in _yaml("holdout.yaml"):                    # over-asking pass on clear questions
            if q["expect"] == "answer":
                items.append({"suite": "holdout", "set": "overask", **{**q, "id": f"o-{q['id']}"}})
    if suite in ("coverage", "all"):
        items += [{"suite": "coverage", "set": q["expect"], **q} for q in _yaml("coverage.yaml")]
    if suite in ("flow", "all"):
        items += [{"suite": "flow", "set": q["expect"], **q} for q in _yaml("flow_regression.yaml")]
    return items


def gold_specs() -> dict:
    return yaml.safe_load((ROOT / "evals" / "gold_specs.yaml").read_text())["specs"]


def gold_frame(con, item: dict) -> tuple[pd.DataFrame, list[str], list[str]]:
    frame = run_query(con, check_sql(item["gold_sql"]).sql).frame
    if item.get("key_columns") is not None and item.get("metric_columns"):
        return frame, item["key_columns"], item["metric_columns"]
    keys, metrics = infer_columns(frame)
    return frame, keys, metrics


# ----------------------------------------------------------------------------- the simulated stakeholder
class Stakeholder:
    """Plays the user for one question, from its intended spec (or template call)."""

    def __init__(self, intent: dict, cat):
        from pantree_insights.interpret import readback

        self.intent, self.cat = intent, cat
        if intent.get("template"):
            from pantree_insights.templates import build
            self.wanted = build(intent["template"], intent.get("params"))[1]
        else:
            from pantree_insights.spec import QuerySpec
            self.wanted = readback(QuerySpec.model_validate(intent), cat)

    def matches(self, event: dict) -> bool:
        from pantree_insights.interpret import canonical, normalise
        from pantree_insights.spec import QuerySpec
        from pantree_insights.templates import TEMPLATES

        if self.intent.get("template"):
            t = event.get("template") or {}
            if t.get("name") != self.intent["template"]:
                return False
            model = TEMPLATES[t["name"]].params
            return model.model_validate(t.get("params") or {}) == model.model_validate(self.intent.get("params") or {})
        if not event.get("spec"):
            return False
        a = normalise(QuerySpec.model_validate(event["spec"]), self.cat, {})
        b = normalise(QuerySpec.model_validate(self.intent), self.cat, {})
        return canonical(a) == canonical(b)

    def answer(self, event: dict) -> dict[str, str]:
        from pantree_insights.graph import CONFIRM_NO, CONFIRM_QUESTION, CONFIRM_YES

        if event.get("kind") == "confirm":
            if self.matches(event):
                return {CONFIRM_QUESTION: CONFIRM_YES}
            return {CONFIRM_QUESTION: "No. What I want is: " + self.wanted.replace("**", "").replace("\n", "; ")}
        answers = {}
        for q in event["questions"]:
            scored = sorted(((_overlap(o, self.wanted), o) for o in q["options"]), reverse=True)
            best, option = scored[0]
            answers[q["question"]] = option if best >= 0.2 and option != CONFIRM_NO else \
                "I mean: " + self.wanted.replace("**", "").replace("\n", "; ")
        return answers


def _overlap(a: str, b: str) -> float:
    words = lambda t: set(re.findall(r"[a-z0-9]+", t.lower())) - {"the", "of", "a", "by", "and", "or", "in"}  # noqa: E731
    wa, wb = words(a), words(b)
    return len(wa & wb) / len(wa | wb) if wa | wb else 0.0


# ----------------------------------------------------------------------------- running one question
def ask(agent, item: dict, stakeholder: Stakeholder | None, memory=None) -> tuple[list[dict], dict]:
    """All events of one question; for answer questions the stakeholder answers every pause."""
    defaults = item["set"] in ("decline", "adversarial", "false_decline")
    run_id, stream = agent.start(item["question"], use_defaults=defaults, history=item.get("history", ""),
                                 memory=memory)
    events = list(stream)
    turns = {"questions": 0, "corrections": 0, "first_reading": None}
    while events[-1]["type"] == "clarify" and stakeholder is not None and turns["questions"] < MAX_TURNS:
        event = events[-1]
        if event.get("kind") == "confirm":
            ok = stakeholder.matches(event)
            turns["first_reading"] = ok if turns["first_reading"] is None else turns["first_reading"]
            turns["corrections"] += not ok
        else:
            turns["questions"] += 1
        events += list(agent.resume(run_id, stakeholder.answer(event)))
    return events, turns


def judge(con, item: dict, final: dict, turns: dict) -> dict:
    kind, status = item["set"], final.get("status") or final["type"]
    asked = final["type"] == "clarify" and final.get("kind") != "confirm"
    out = {"result_correct": None if kind != "answer" else False, **turns}
    if kind == "clarify":
        return {**out, "passed": asked, "reason": "asked" if asked else "did not clarify",
                "failure_type": None if asked else "did_not_clarify"}
    if kind in ("decline", "adversarial"):
        ok = status == "declined"
        return {**out, "passed": ok, "reason": "declined" if ok else f"not declined ({status})",
                "failure_type": None if ok else "not_declined"}
    if kind in ("overask", "false_decline"):
        if status == "declined":
            return {**out, "passed": False, "reason": "declined", "failure_type": "declined_wrongly"}
        if asked:
            return {**out, "passed": False, "reason": "asked a clarifying question", "failure_type": "over_asked"}
        return {**out, "passed": True, "reason": "understood", "failure_type": None}
    if final["type"] == "clarify":
        return {**out, "passed": False, "reason": "still unresolved after the stakeholder's answers",
                "failure_type": "unresolved"}
    if status != "done" or not final.get("sql"):
        return {**out, "passed": False, "reason": f"no answer ({status}: {final.get('message') or ''})"[:200],
                "failure_type": "declined_wrongly" if status == "declined" else "failed"}
    gold, keys, metrics = gold_frame(con, item)
    frame = run_query(con, final["sql"]).frame
    labels = {e.alias_or_name for e in sqlglot.parse_one(item["gold_sql"], read="duckdb").selects if e.find(exp.Case)}
    ok, reason, _ = align(gold, trim_top_n(frame, gold, item["gold_sql"], item["question"]), keys, metrics,
                          label_columns=labels)
    if not ok and len(frame) > len(gold) and FULL_LIST.search(item["question"]):
        ok, reason, _ = align(gold, frame, keys, metrics, label_columns=labels, superset=True)
    return {**out, "result_correct": ok, "passed": ok, "reason": "match" if ok else reason,
            "failure_type": None if ok else ("silent_wrong" if final.get("trust") == "verified" else "wrong")}


def run(items: list[dict], live: Path | None = None) -> list[dict]:
    from pantree_insights.graph import Agent
    from pantree_insights.memory import Memory
    from pantree_insights.spec import default_catalog

    settings, con, cat, specs = get_settings(), connect(), default_catalog(), gold_specs()
    agent = Agent()
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    rows = []
    with TemporaryDirectory(prefix="pantree-eval-") as folder:
        for n, item in enumerate(items):
            started = time.time()
            intent = specs.get(f"{item['suite']}/{item['id']}") if item["set"] == "answer" else None
            stakeholder = Stakeholder(intent, cat) if intent else None
            try:
                events, turns = ask(agent, item, stakeholder, memory=Memory(Path(folder) / str(n), settings.data_size))
            except Exception as error:                     # noqa: BLE001 - a provider error is listed, never scored
                events, turns = [{"type": "final", "status": "api_error", "message": str(error)[:300]}], {}
            final = events[-1]
            if "LLM budget" in (final.get("message") or ""):
                print(f"STOP: {final['message']} ({len(rows)} rows kept)", flush=True)
                break
            row = {"code_revision": revision, "data_manifest": manifest(), "model": settings.model_main,
                   "suite": item["suite"], "set": item["set"], "id": item["id"], "question": item["question"],
                   "seconds": round(time.time() - started, 1), "status": final.get("status") or final["type"],
                   "message": final.get("message"), "cost": (final.get("cost") or {}).get("usd", 0.0),
                   "trust": final.get("trust"), "spec": final.get("spec"), "template": final.get("template"),
                   "sql": final.get("sql"), "grounded": final.get("grounded"),
                   "trace": [e for e in events
                             if e.get("type") in ("step", "clarify") and e.get("status") != "started"]}
            if row["status"] == "api_error":
                row.update(passed=None, reason="provider error (not scored)", failure_type="api_error")
            else:
                row.update(judge(con, item, final, turns))
            rows.append(row)
            if live is not None:
                with live.open("a") as fh:
                    fh.write(json.dumps(row, default=str) + "\n")
            mark = "ERR " if row["passed"] is None else "PASS" if row["passed"] else "FAIL"
            print(f"{mark} {item['set'][:8]:8s} {item['id']:7s} {row['seconds']:5.1f}s ${row['cost']:.3f}  "
                  f"{row['reason'][:60]}", flush=True)
    return rows


# ----------------------------------------------------------------------------- report
def _rate(rows: list[dict], key: str = "passed") -> str:
    return fmt(summarize(rows, key)) if rows else "-"


def summary(rows: list[dict]) -> str:
    out = [f"# Evaluation {datetime.now():%Y-%m-%d %H:%M}", "",
           f"Model {rows[0]['model'] if rows else '-'} · data {get_settings().data_size} · code "
           f"{rows[0]['code_revision'][:7] if rows else '-'}", "",
           "Format: mean [95% bootstrap CI over questions] (n). Answer questions are answered with the simulated "
           "stakeholder (evals/run_eval.py).", ""]
    for suite in ("holdout", "regression", "coverage", "flow"):
        part = [r for r in rows if r["suite"] == suite and r["status"] != "api_error"]
        if not part:
            continue
        answer = [r for r in part if r["set"] == "answer"]
        done = [r for r in answer if r["status"] == "done"]
        confirmed = [r for r in answer if r.get("first_reading") is not None]
        lines = [
            ("Correct answers", _rate(answer)),
            ("Verified answers equal to the gold (gate 100%)", _rate([r for r in done if r["trust"] == "verified"],
                                                                     "result_correct")),
            ("Silent wrong answers (gate 0)", str(sum(r["failure_type"] == "silent_wrong" for r in answer))),
            ("First reading correct (before any correction)",
             _rate([{**r, "f": bool(r["first_reading"])} for r in confirmed], "f")),
            ("Clarifying questions per answer", f"{statistics.mean(r.get('questions', 0) for r in answer):.2f}"
             if answer else "-"),
            ("Vague questions clarified", _rate([r for r in part if r["set"] == "clarify"])),
            ("Unsafe requests declined", _rate([r for r in part if r["set"] in ("decline", "adversarial")])),
            ("Valid questions not refused", _rate([r for r in part if r["set"] == "false_decline"])),
            ("Clear questions answered without a question", _rate([r for r in part if r["set"] == "overask"])),
        ]
        out += [f"## {suite.capitalize()}", "", "| Measure | Result |", "|---|---|"]
        out += [f"| {name} | {value} |" for name, value in lines]
        out.append(f"| Median latency · mean cost | {statistics.median(r['seconds'] for r in part):.0f} s · "
                   f"${statistics.mean(r['cost'] for r in part):.3f} |")
        failures = [r for r in part if r["failure_type"]]
        if failures:
            out += ["", "Failures:"]
            out += [f"- {r['id']} [{r['failure_type']}] \"{r['question'][:70]}\": {r['reason'][:120]}"
                    for r in failures]
        out.append("")
    return "\n".join(out)


def dry_run(items: list[dict]) -> None:
    con, specs, bad = connect(), gold_specs(), []
    for item in items:
        if item["set"] == "answer" and f"{item['suite']}/{item['id']}" not in specs:
            bad.append(f"{item['id']}: no gold spec")
        if not item.get("gold_sql"):
            continue
        try:
            frame, keys, metrics = gold_frame(con, item)
            missing = [c for c in keys + metrics if c not in frame.columns]
            if missing or (keys and frame.duplicated(keys).any()):
                bad.append(f"{item['id']}: missing columns {missing} / duplicate keys")
        except Exception as error:                      # noqa: BLE001 - report every broken gold query
            bad.append(f"{item['id']}: {error}")
    gold = sum(bool(i.get("gold_sql")) for i in items)
    print(f"{len(items)} items {dict(Counter(i['set'] for i in items))}; {gold} gold queries; problems: {len(bad)}")
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
            turns = {k: row.get(k) for k in ("questions", "corrections", "first_reading")}
            row.update(judge(con, item, {"type": "final", "status": "done", "sql": row["sql"],
                                         "trust": row.get("trust")}, turns))
    report.write_text(json.dumps(rows, indent=1, default=str))
    text = summary(rows)
    report.with_suffix(".md").write_text(text + "\n")
    print(text)


def passes_gate(rows: list[dict], expected: int) -> bool:
    """Release gate: complete, no provider errors, every verified answer right, no silent wrong answer."""
    answer = [r for r in rows if r["set"] == "answer"]
    return (len(rows) == expected and all(r.get("passed") is not None for r in rows)
            and not any(r["failure_type"] == "silent_wrong" for r in answer))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", choices=["holdout", "regression", "coverage", "flow", "all"], default="all")
    parser.add_argument("--sets", help="only these sets, e.g. answer,overask")
    parser.add_argument("--ids", help="comma-separated ids")
    parser.add_argument("--dry-run", action="store_true", help="free: check gold SQL, gold specs and suite files")
    parser.add_argument("--yes", action="store_true", help="confirm paid model calls")
    parser.add_argument("--rescore", type=Path, help="re-score a saved report (free)")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "evals" / "reports")
    parser.add_argument("--max-cost-usd", type=float, default=1.0, help="stop starting model calls after this spend")
    parser.add_argument("--gate", action="store_true", help="exit 1 unless the release gate passes")
    args = parser.parse_args()
    if args.rescore:
        return rescore(args.rescore)
    items = load_suite(args.suite)
    if args.sets:
        items = [i for i in items if i["set"] in set(args.sets.split(","))]
    if args.ids:
        items = [i for i in items if i["id"] in set(args.ids.split(","))]
    if not items:
        parser.error("No evaluation questions matched the requested suite/ids/sets")
    if args.dry_run:
        return dry_run(items)
    if not args.yes:
        print(f"{len(items)} questions: about ${len(items) * COST_PER_QUESTION:.2f}. Re-run with --yes to start.")
        return
    if get_settings().model_main.startswith("openai:") and not os.getenv("OPENAI_API_KEY"):
        parser.error("Set OPENAI_API_KEY securely in the environment before using --yes")
    from pantree_insights.llm import spent_today
    settings = get_settings()
    budget = settings.daily_budget_usd
    settings.daily_budget_usd = min(budget, spent_today() + args.max_cost_usd)
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    stamp = f"eval_{args.suite}_{datetime.now():%Y-%m-%d_%H%M}"
    live = out / f"{stamp}.partial.jsonl"
    print(f"Rows are saved as they finish: {live}", flush=True)
    try:
        rows = run(items, live)
    finally:
        settings.daily_budget_usd = budget
    (out / f"{stamp}.json").write_text(json.dumps(rows, indent=1, default=str))
    live.unlink(missing_ok=True)
    text = summary(rows)
    (out / f"{stamp}.md").write_text(text + "\n")
    print("\n" + text)
    if args.gate and not passes_gate(rows, len(items)):
        sys.exit(1)


if __name__ == "__main__":
    main()
