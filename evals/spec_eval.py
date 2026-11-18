"""Free check of the v2 compiler: every gold QuerySpec must validate, compile and return the gold answer.

    DATA_SIZE=full uv run python evals/spec_eval.py            # report coverage and correctness
    DATA_SIZE=full uv run python evals/spec_eval.py --show x06 # print the compiled SQL of one question

Coverage = share of answer questions whose gold spec is routed to the compiler.
Correctness = share of those whose compiled result matches the gold SQL (same scorer as the paid evaluation).
No LLM is involved: this measures the compiler and the catalog, not the model's reading of the question.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import sqlglot
import yaml
from sqlglot import exp

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))

from run_eval import FULL_LIST, gold_frame, load_suite  # noqa: E402
from scoring import align, trim_top_n  # noqa: E402

from pantree_insights.db import run_query  # noqa: E402
from pantree_insights.spec import QuerySpec, compile_spec, default_catalog, validate  # noqa: E402
from pantree_insights.templates import TemplateError  # noqa: E402
from pantree_insights.templates import build as build_template  # noqa: E402


def load_gold_specs() -> dict[str, dict]:
    return yaml.safe_load((ROOT / "evals" / "gold_specs.yaml").read_text())["specs"]


def check(item: dict, raw: dict, cat) -> dict:
    """{route, ok, reason, sql} for one question."""
    started = time.perf_counter()
    if raw.get("template"):
        route = "template"
        try:
            sql, _ = build_template(raw["template"], raw.get("params"))
        except TemplateError as error:
            return {"route": route, "ok": False, "reason": str(error), "sql": None}
    else:
        route = "compiled"
        spec = QuerySpec.model_validate(raw)
        problems = validate(spec, cat)
        if problems:
            return {"route": route, "ok": False, "reason": "invalid: " + "; ".join(problems), "sql": None}
        sql = compile_spec(spec, cat)
    compile_seconds = time.perf_counter() - started
    try:
        result = run_query(cat.con, sql)
        frame = result.frame
        if result.truncated:
            return {"route": route, "ok": False, "reason": "result exceeded the row limit", "sql": sql}
    except Exception as error:                         # noqa: BLE001 - reported, never hidden
        return {"route": route, "ok": False, "reason": f"database error: {error}", "sql": sql}
    gold, keys, metrics = gold_frame(cat.con, item)
    tree = sqlglot.parse_one(item["gold_sql"], read="duckdb")
    labels = {e.alias_or_name for e in tree.selects if e.find(exp.Case)}
    agent = trim_top_n(frame, gold, item["gold_sql"], item["question"])
    options = {"percent_metrics": set(item.get("percent_metrics", [])), "tolerances": item.get("metric_tolerances")}
    ok, reason, _ = align(gold, agent, keys, metrics, label_columns=labels, **options)
    if not ok and len(frame) > len(gold) and FULL_LIST.search(item["question"]):
        ok, reason, _ = align(gold, frame, keys, metrics, label_columns=labels, superset=True, **options)
    return {"route": route, "ok": ok, "reason": reason, "sql": sql,
            "compile_seconds": compile_seconds, "query_seconds": result.seconds}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--show", help="question id: print its compiled SQL and result check")
    args = parser.parse_args()
    cat = default_catalog()
    specs = load_gold_specs()
    items = [i for i in load_suite("all") if i["set"] == "answer"]
    missing = [f"{i['suite']}/{i['id']}" for i in items if f"{i['suite']}/{i['id']}" not in specs]
    if missing:
        sys.exit(f"no gold spec for: {', '.join(missing)}")
    rows = []
    for item in items:
        key = f"{item['suite']}/{item['id']}"
        if args.show and item["id"] != args.show:
            continue
        result = check(item, specs[key], cat)
        rows.append((key, result))
        if args.show:
            print(result["sql"] or "(no SQL)")
            print(result["reason"])
    verified = [r for _, r in rows if r["route"] in ("compiled", "template")]
    right = [r for r in verified if r["ok"]]
    templates = sum(r["route"] == "template" for _, r in rows)
    print(f"\nanswer questions: {len(rows)} (compiled {len(verified) - templates}, templates {templates})")
    print(f"verified results matching gold: {len(right)}/{len(verified)}")
    for key, r in rows:
        if not r["ok"]:
            print(f"  FAIL {key}: {r['reason'][:200]}")
    if len(right) != len(verified) or len(verified) != len(rows):
        sys.exit(1)


if __name__ == "__main__":
    main()
