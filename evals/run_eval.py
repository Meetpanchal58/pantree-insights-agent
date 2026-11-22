"""Autonomous or stakeholder-assisted evaluation (paid: real models), plus free compiler checks.

    uv run python evals/run_eval.py --suite all --dry-run          # free: gold SQL and suite files
    uv run python evals/run_eval.py --suite all                    # prints the cost estimate
    uv run python evals/run_eval.py --suite all --yes --max-cost-usd 4
    uv run python evals/run_eval.py --rescore evals/reports/<file>.json

Suites: regression (evals/regression.yaml + questions.yaml), holdout (holdout.yaml + behaviour.yaml + an
over-asking pass), coverage (coverage.yaml), flow (flow_regression.yaml; follow-ups with chat history).

Autonomous mode is the default: gold judges but does not steer. A scripted stakeholder blindly confirms plans.
--mode user plays a business user: on answer questions it picks one of the options the agent offers (never free
text, so no spec is leaked) and still confirms plans blindly; vague, refusal and over-asking items are judged on
the agent's first response. --mode assisted adds gold-guided corrections. Never mix modes in one accuracy report.
Numerical checks are not a human narrative-faithfulness score.

Measures (docs/ARCHITECTURE.md): verified answers equal to the gold (gate: 100%), silent wrong answers (gate: 0),
first reading correct, questions per answer, and the behaviour sets (declines, vague questions, over-asking).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
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

from provenance import SCORER_VERSION, cases_digest, data_digest, latency, provenance, stage_seconds  # noqa: E402
from scoring import align, infer_columns, trim_top_n, winner_only  # noqa: E402
from stats import fmt, summarize  # noqa: E402

from pantree_insights.config import get_settings  # noqa: E402
from pantree_insights.db import connect, run_query  # noqa: E402
from pantree_insights.guardrails import check_sql  # noqa: E402

COST_PER_QUESTION = 0.04       # planning allowance only; actual cost depends on calls and token usage
FULL_LIST = re.compile(r"\b(each|every|per)\b", re.I)
MAX_TURNS = 5                  # stakeholder answers per question before giving up


def _yaml(name: str, key: str = "questions") -> list[dict]:
    return yaml.safe_load((ROOT / "evals" / name).read_text())[key] or []


def load_suite(suite: str) -> list[dict]:
    items: list[dict] = []
    if suite in ("regression", "all"):
        for e in _yaml("regression.yaml", "examples"):
            items.append({"suite": "regression", "set": "answer", "expect": "answer", "id": e["id"],
                          "question": e["question"], "gold_sql": e["sql"], "difficulty": e.get("difficulty"),
                          **{k: e[k] for k in ("key_columns", "metric_columns") if k in e}})
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
    verdict = check_sql(item["gold_sql"])
    if not verdict.ok:
        raise ValueError("Invalid gold SQL: " + "; ".join(verdict.errors))
    result = run_query(con, verdict.sql)
    if result.truncated:
        raise ValueError("Gold SQL exceeded the row limit; a partial reference cannot score an answer")
    frame = result.frame
    if item.get("key_columns") is not None and item.get("metric_columns") is not None:
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

    def choose(self, event: dict) -> dict[str, str]:
        """A business user's reply: the offered option closest to what they want, never a written correction."""
        return {q["question"]: max(q["options"], key=lambda o: _overlap(o, self.wanted)) for q in event["questions"]}


def _overlap(a: str, b: str) -> float:
    words = lambda t: set(re.findall(r"[a-z0-9]+", t.lower())) - {"the", "of", "a", "by", "and", "or", "in"}  # noqa: E731
    wa, wb = words(a), words(b)
    return len(wa & wb) / len(wa | wb) if wa | wb else 0.0


# ----------------------------------------------------------------------------- running one question
def ask(agent, item: dict, stakeholder: Stakeholder | None, memory=None,
        mode: str = "autonomous") -> tuple[list[dict], dict]:
    """Gold judges the initial response; only explicitly assisted runs receive corrections."""
    run_id, stream = agent.start(item["question"], history=item.get("history", ""),
                                 memory=memory)
    events = list(stream)
    turns = {"questions": 0, "corrections": 0, "first_reading": None, "interactions": 0}
    first = events[-1]
    turns["first_turn_status"] = first.get("status") or first["type"]
    turns["first_turn_correct"] = (stakeholder.matches(first) if stakeholder is not None
                                   and (first.get("spec") or first.get("template")) else False)
    turns["first_turn_answered"] = first.get("status") == "done"
    if (first.get("status") == "done" or first.get("kind") == "confirm") and stakeholder is not None:
        turns["first_reading"] = turns["first_turn_correct"]
    turns["first_turn_plan_ready"] = first.get("kind") == "confirm"
    turns["confirmation_compliant"] = not any(e["type"] == "result" for e in events)
    turns["confirmations"] = 0
    while events[-1]["type"] == "clarify" and turns["interactions"] < MAX_TURNS:
        event = events[-1]
        if event.get("kind") == "confirm":
            turns["confirmations"] += 1
            if mode in ("autonomous", "user"):
                reply = "Go ahead"  # deliberately no gold-guided corrections
            elif stakeholder is not None:
                ok = stakeholder.matches(event)
                turns["corrections"] += not ok
                reply = stakeholder.answer(event)
            else:
                reply = "Go ahead"
        elif mode == "assisted" and stakeholder is not None:
            turns["questions"] += 1
            reply = stakeholder.answer(event)
        elif mode == "user" and stakeholder is not None and item["set"] == "answer" and all(
                q.get("options") for q in event.get("questions") or [{}]):
            turns["questions"] += 1
            reply = stakeholder.choose(event)
        else:
            break
        turns["interactions"] += 1
        events += list(agent.resume(run_id, reply, plan_version=event.get("plan_version")))
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
        understood = ((final["type"] == "clarify" and final.get("kind") == "confirm"
                       and bool(final.get("spec") or final.get("template")))
                      or (status == "done" and bool(final.get("sql"))))
        return {**out, "passed": understood, "reason": "understood" if understood else f"not completed ({status})",
                "failure_type": None if understood else "failed"}
    if final["type"] == "clarify":
        return {**out, "passed": False, "reason": "still unresolved after the stakeholder's answers",
                "failure_type": "unresolved"}
    if status != "done" or not final.get("sql"):
        return {**out, "passed": False, "reason": f"no answer ({status}: {final.get('message') or ''})"[:200],
                "failure_type": "declined_wrongly" if status == "declined" else "failed"}
    gold, keys, metrics = gold_frame(con, item)
    result = run_query(con, final["sql"])
    if result.truncated:
        return {**out, "passed": False, "reason": "result exceeded the row limit", "failure_type": "truncated"}
    frame = result.frame
    labels = {e.alias_or_name for e in sqlglot.parse_one(item["gold_sql"], read="duckdb").selects if e.find(exp.Case)}
    ok, reason, _ = align(gold, trim_top_n(frame, gold, item["gold_sql"], item["question"]), keys, metrics,
                          label_columns=labels, percent_metrics=set(item.get("percent_metrics", [])),
                          tolerances=item.get("metric_tolerances"))
    if not ok and metrics and len(frame) < len(gold):
        winners = winner_only(frame, gold, metrics[0], item["question"])
        if len(winners) < len(gold):
            ok, reason, _ = align(winners, frame, keys, metrics, label_columns=labels,
                                  percent_metrics=set(item.get("percent_metrics", [])),
                                  tolerances=item.get("metric_tolerances"))
    if not ok and len(frame) > len(gold) and FULL_LIST.search(item["question"]):
        ok, reason, _ = align(gold, frame, keys, metrics, label_columns=labels, superset=True,
                              percent_metrics=set(item.get("percent_metrics", [])),
                              tolerances=item.get("metric_tolerances"))
    return {**out, "result_correct": ok, "passed": ok, "reason": "match" if ok else reason,
            "failure_type": None if ok else ("silent_wrong" if final.get("trust") == "verified" else "wrong")}


def run(items: list[dict], live: Path | None = None, repeats: int = 1, mode: str = "autonomous",
        llm_factory=None, should_stop=None) -> list[dict]:
    from pantree_insights.graph import Agent
    from pantree_insights.memory import Memory
    from pantree_insights.spec import default_catalog

    settings = get_settings()
    inputs = provenance(items, settings.shop_dir)
    prepared = time.perf_counter()
    con, cat, specs = connect(), default_catalog(), gold_specs()
    agent = Agent(llm_factory=llm_factory) if llm_factory else Agent()
    preparation_seconds = time.perf_counter() - prepared
    rows = []
    with TemporaryDirectory(prefix="pantree-eval-") as folder:
        for n, (repeat, item) in enumerate((r, i) for r in range(repeats) for i in items):
            started = time.perf_counter()
            intent = specs.get(f"{item['suite']}/{item['id']}") if item["set"] == "answer" else None
            stakeholder = Stakeholder(intent, cat) if intent else None
            try:
                events, turns = ask(agent, item, stakeholder,
                                    memory=Memory(Path(folder) / str(n), settings.data_size), mode=mode)
            except Exception as error:                     # noqa: BLE001 - failed attempts stay in the denominator
                status = "api_error" if type(error).__module__.split(".")[0] == "openai" else "execution_error"
                events, turns = [{"type": "final", "status": status, "message": str(error)[:300]}], {}
            final = events[-1]
            if "LLM budget" in (final.get("message") or ""):
                print(f"STOP: {final['message']} ({len(rows)} rows kept)", flush=True)
                break
            row = {**inputs, "evaluation_mode": mode, "model": settings.model_main, "repeat": repeat,
                   "preparation_seconds": preparation_seconds, "memory_mode": "isolated_per_question",
                   "cache_mode": "shared_agent_sql_cache; provider_cache_uncontrolled",
                   "suite": item["suite"], "set": item["set"], "id": item["id"], "question": item["question"],
                   "seconds": round(time.perf_counter() - started, 3), "status": final.get("status") or final["type"],
                   "final_type": final["type"], "final_kind": final.get("kind"),
                   "stage_seconds": stage_seconds(events), "calls": final.get("calls", []),
                   "retrieval_seconds": stage_seconds(events).get("context"),
                   "narrative_faithfulness": None,  # requires independent semantic review, not lexical checks
                   "message": final.get("message"), "cost": (final.get("cost") or {}).get("usd"),
                   "trust": final.get("trust"), "spec": final.get("spec"), "template": final.get("template"),
                   "sql": final.get("sql"), "grounded": final.get("grounded"),
                   "trace": [e for e in events
                             if e.get("type") in ("step", "clarify") and e.get("status") != "started"]}
            if row["status"] in ("api_error", "execution_error"):
                row.update(passed=False, result_correct=False if item["set"] == "answer" else None,
                           reason=row["status"], failure_type=row["status"])
            else:
                try:
                    row.update(judge(con, item, final, turns))
                except Exception as error:  # noqa: BLE001 - retain the failed case and continue the evaluation
                    row.update(passed=False, result_correct=False, failure_type="evaluation_error",
                               reason=f"Could not score: {str(error)[:200]}")
            row["final_intent_correct"] = bool(stakeholder and (final.get("spec") or final.get("template"))
                                               and stakeholder.matches(final))
            row["plan_sql_evidence_correct"] = (bool(row.get("result_correct") and row["final_intent_correct"]
                                                     and row.get("grounded")) if item["set"] == "answer" else None)
            rows.append(row)
            if live is not None:
                with live.open("a") as fh:
                    fh.write(json.dumps(row, default=str) + "\n")
            mark = "PASS" if row["passed"] else "FAIL"
            cost = f"${row['cost']:.3f}" if row["cost"] is not None else "cost unknown"
            print(f"{mark} {item['set'][:8]:8s} {item['id']:7s} {row['seconds']:5.1f}s {cost}  "
                  f"{row['reason'][:60]}", flush=True)
            if should_stop and should_stop():
                break
    agent.close()
    unchanged = data_digest(settings.shop_dir) == inputs["data_sha256"]
    for row in rows:
        row["snapshot_verified"] = unchanged
        if not unchanged:
            row.update(passed=False, result_correct=False, failure_type="data_changed",
                       reason="Data changed during evaluation; discard this run")
    return rows


# ----------------------------------------------------------------------------- report
def _rate(rows: list[dict], key: str = "passed") -> str:
    return fmt(summarize(rows, key)) if rows else "-"


def summary(rows: list[dict]) -> str:
    modes = {r.get("evaluation_mode", "legacy_assisted") for r in rows}
    if len(modes) > 1:
        raise ValueError("evaluation modes must be reported separately")
    out = [f"# Evaluation {datetime.now():%Y-%m-%d %H:%M}", "",
           f"Model {rows[0]['model'] if rows else '-'} · data {get_settings().data_size} · code "
           f"{rows[0]['code_revision'][:7] if rows else '-'}", "",
           "Format: mean [95% bootstrap CI over questions; Wilson at all-pass/all-fail boundaries] (n). "
           f"Evaluation mode: {next(iter(modes), 'no rows')}. "
           "Autonomous confirmation is blind; unresolved ambiguities count as incomplete.", ""]
    for suite in ("holdout", "regression", "coverage", "flow"):
        part = [r for r in rows if r["suite"] == suite]
        if not part:
            continue
        answer = [r for r in part if r["set"] == "answer"]
        done = [r for r in answer if r["status"] == "done"]
        confirmed = [r for r in answer if r.get("first_reading") is not None]
        lines = [
            ("Correct result tables (all attempts, including provider errors)", _rate(answer)),
            ("Completed answer coverage", _rate([{**r, "completed": r["status"] == "done"} for r in answer],
                                               "completed")),
            ("Provider errors", str(sum(r["status"] == "api_error" for r in part))),
            ("Execution errors", str(sum(r["status"] == "execution_error" for r in part))),
            ("Verified answers equal to the gold (gate 100%)", _rate([r for r in done if r["trust"] == "verified"],
                                                                     "result_correct")),
            ("Silent wrong answers (gate 0)", str(sum(r["failure_type"] == "silent_wrong" for r in answer))),
            ("First reading correct (before any correction)",
             _rate([{**r, "f": bool(r["first_reading"])} for r in confirmed], "f")),
            ("First-turn intended plan (all answer questions, before stakeholder assistance)",
             _rate([{**r, "f": bool(r.get('first_turn_correct'))} for r in answer], "f")),
            ("First-turn plan ready for confirmation", _rate(answer, "first_turn_plan_ready")),
            ("No result before confirmation", _rate(answer, "confirmation_compliant")),
            ("Exact intended plan + gold table + checked evidence (not full narrative review)",
             _rate(answer, "plan_sql_evidence_correct")),
            ("Independent narrative faithfulness", "not measured"),
            ("Clarifying questions per answer", f"{statistics.mean(r.get('questions', 0) for r in answer):.2f}"
             if answer else "-"),
            ("Vague questions clarified", _rate([r for r in part if r["set"] == "clarify"])),
            ("Unsafe requests declined", _rate([r for r in part if r["set"] in ("decline", "adversarial")])),
            ("Valid questions not refused", _rate([r for r in part if r["set"] == "false_decline"])),
            ("Clear questions understood without extra clarification (confirmation allowed)",
             _rate([r for r in part if r["set"] == "overask"])),
        ]
        out += [f"## {suite.capitalize()}", "", "| Measure | Result |", "|---|---|"]
        out += [f"| {name} | {value} |" for name, value in lines]
        costs = [r["cost"] for r in part if r.get("cost") is not None]
        cost = f"${statistics.mean(costs):.3f}" if costs else "unknown"
        out.append(f"| Median latency · mean known cost ({len(costs)}/{len(part)} recorded) | "
                   f"{statistics.median(r['seconds'] for r in part):.0f} s · {cost} |")
        timing = latency([r["seconds"] for r in part])
        out.append(f"| All-attempt latency p95 · max (n={timing['n']}) | "
                   f"{timing['p95']:.2f} s · {timing['max']:.2f} s |")
        for stage in sorted({s for r in part for s in r.get("stage_seconds", {})}):
            timing = latency([r["stage_seconds"][stage] for r in part if stage in r.get("stage_seconds", {})])
            out.append(f"| {stage} p50 / p95 (n={timing['n']}) | {timing['p50']:.2f} / {timing['p95']:.2f} s |")
        failures = [r for r in part if r["failure_type"]]
        if failures:
            out += ["", "Failures:"]
            out += [f"- {r['id']} [{r['failure_type']}] \"{r['question'][:70]}\": {r['reason'][:120]}"
                    for r in failures]
        out.append("")
    out += ["Startup preparation is recorded separately in JSON; request timings exclude it and browser/network time.",
            "Retrieval latency is unavailable until a standalone retrieval stage exists.",
            "Behaviour passes measure routing, not numeric correctness. "
            "Result-table checks do not grade narrative claims.",
            "Bootstrap intervals describe this question sample; "
            "all-pass samples do not establish 100% population accuracy."]
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
    digest = data_digest(get_settings().shop_dir)
    if not rows or any(r.get("data_sha256") != digest or not r.get("snapshot_verified") for r in rows):
        raise ValueError("Cannot rescore: report lacks a verified matching data fingerprint. Run a new evaluation.")
    by_id = {(i["suite"], i["id"]): i for i in load_suite("all")}
    selected = list(dict.fromkeys((r["suite"], r["id"]) for r in rows))
    cases = cases_digest([by_id[key] for key in selected])
    if any(r.get("cases_sha256") != cases for r in rows):
        raise ValueError("Cannot rescore: evaluation cases changed or run was incomplete. Run a new evaluation.")
    con = connect()
    for row in rows:
        item = by_id[(row["suite"], row["id"])]
        if row["status"] not in ("api_error", "execution_error"):
            turns = {k: row.get(k) for k in ("questions", "corrections", "first_reading")}
            row.update(judge(con, item, {"type": row.get("final_type", "final"), "kind": row.get("final_kind"),
                                         "status": row["status"], "sql": row.get("sql"), "spec": row.get("spec"),
                                         "template": row.get("template"), "trust": row.get("trust")}, turns))
        row["rescored_with"] = SCORER_VERSION
    if data_digest(get_settings().shop_dir) != digest:
        raise ValueError("Data changed during rescoring; no report written")
    output = report.with_name(report.stem + ".rescored.json")
    output.write_text(json.dumps(rows, indent=1, default=str))
    text = summary(rows)
    output.with_suffix(".md").write_text(text + "\n")
    print(text)


def passes_gate(rows: list[dict], expected: int) -> bool:
    """A release baseline must complete and pass every requested case, including behaviour cases."""
    return (expected > 0 and len(rows) == expected
            and all(r.get("passed") is True and not r.get("failure_type")
                    and r.get("snapshot_verified", True) for r in rows))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["autonomous", "user", "assisted"], default="autonomous",
                        help="autonomous confirms plans blindly; user also picks offered options; assisted corrects")
    parser.add_argument("--suite", choices=["holdout", "regression", "coverage", "flow", "all"], default="all")
    parser.add_argument("--sets", help="only these sets, e.g. answer,overask")
    parser.add_argument("--ids", help="comma-separated ids")
    parser.add_argument("--dry-run", action="store_true", help="free: check gold SQL, gold specs and suite files")
    parser.add_argument("--yes", action="store_true", help="confirm paid model calls")
    parser.add_argument("--repeats", type=int, default=1, help="repeat each case; isolated memory, shared SQL cache")
    parser.add_argument("--rescore", type=Path, help="re-score a saved report (free)")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "evals" / "reports")
    parser.add_argument("--max-cost-usd", type=float, default=1.0, help="stop starting model calls after this spend")
    parser.add_argument("--gate", action="store_true", help="exit 1 unless the release gate passes")
    args = parser.parse_args()
    if args.repeats < 1 or not 0 < args.max_cost_usd < float("inf"):
        parser.error("--repeats and --max-cost-usd must be positive and finite")
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
        print(f"{len(items)} questions x {args.repeats} repeats: about "
              f"${len(items) * args.repeats * COST_PER_QUESTION:.2f} (planning allowance, not a measured forecast). "
              "Re-run with --yes to start.")
        return
    if get_settings().model_main.startswith("openai:") and not os.getenv("OPENAI_API_KEY"):
        parser.error("Set OPENAI_API_KEY securely in the environment before using --yes")
    from pantree_insights.llm import spent_today
    settings = get_settings()
    budget = settings.daily_budget_usd
    settings.daily_budget_usd = min(budget, spent_today() + args.max_cost_usd)
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    stamp = f"eval_{args.suite}_{datetime.now():%Y-%m-%d_%H%M%S_%f}"
    live = out / f"{stamp}.partial.jsonl"
    print(f"Rows are saved as they finish: {live}", flush=True)
    try:
        rows = run(items, live, args.repeats, mode=args.mode)
    finally:
        settings.daily_budget_usd = budget
    (out / f"{stamp}.json").write_text(json.dumps(rows, indent=1, default=str))
    live.unlink(missing_ok=True)
    text = summary(rows)
    (out / f"{stamp}.md").write_text(text + "\n")
    print("\n" + text)
    if args.gate and not passes_gate(rows, len(items) * args.repeats):
        sys.exit(1)


if __name__ == "__main__":
    main()
