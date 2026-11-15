"""Free retrieval evaluation (no LLM): does retrieval return the context each question needs?

    uv run python evals/retrieval_eval.py

Suites:
  holdout     evals/holdout.yaml answer questions with annotated required tables, metrics, joins, values
  regression  semantic/examples.yaml, leave-one-out (each question's own example is hidden), with required
              tables and joins derived from its gold SQL
K = what the agent receives: up to 6 tables, 4 metrics, 8 values, and the approved joins among the tables.
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import sqlglot
import yaml
from sqlglot import exp

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pantree_insights.config import get_settings  # noqa: E402
from pantree_insights.retrieval import get_retriever  # noqa: E402
from pantree_insights.semantic import load_examples, load_schema, tables_in  # noqa: E402


def norm_join(join: str) -> frozenset:
    return frozenset(side.strip().lower() for side in join.split("="))


def joins_in(sql: str) -> set[frozenset]:
    """Approved equality joins used by a gold query."""
    schema = load_schema()
    approved = {norm_join(j) for j in schema.joins}
    tree = sqlglot.parse_one(sql, read="duckdb")
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    alias = {t.alias_or_name.lower(): t.name.lower() for t in tree.find_all(exp.Table)
             if t.name.lower() in schema.tables and t.name.lower() not in ctes}
    found = set()
    for join in tree.find_all(exp.Join):
        right = alias.get(join.this.alias_or_name.lower()) if isinstance(join.this, exp.Table) else None
        on = join.args.get("on")
        for eq in (on.find_all(exp.EQ) if on is not None else []):
            a, b = eq.left, eq.right
            if isinstance(a, exp.Column) and isinstance(b, exp.Column) and a.table and b.table:
                ta, tb = alias.get(a.table.lower()), alias.get(b.table.lower())
                if ta and tb and ta != tb:
                    pair = frozenset({f"{ta}.{a.name.lower()}", f"{tb}.{b.name.lower()}"})
                    if pair in approved:
                        found.add(pair)
        for ident in join.args.get("using") or []:
            for other in set(alias.values()) - {right}:
                pair = frozenset({f"{other}.{ident.name.lower()}", f"{right}.{ident.name.lower()}"})
                if pair in approved:
                    found.add(pair)
    return found


def items() -> list[dict]:
    out = []
    allowed = set(load_schema().tables)
    for e in load_examples().examples:
        out.append({"suite": "regression", "id": e.id, "question": e.question, "exclude": {e.id},
                    "tables": tables_in(e.sql) & allowed, "joins": joins_in(e.sql), "metrics": None, "values": None})
    for q in yaml.safe_load((ROOT / "evals" / "holdout.yaml").read_text())["questions"]:
        if q["expect"] != "answer":
            continue
        out.append({"suite": "holdout", "id": q["id"], "question": q["question"], "exclude": set(),
                    "tables": set(q["required_tables"]), "joins": {norm_join(j) for j in q["required_joins"]},
                    "metrics": set(q["required_metrics"]),
                    "values": {(v["value"], v["column"]) for v in q.get("filter_values", [])}})
    return out


def pr(required: set, got: set) -> tuple[float | None, float | None]:
    if not required:
        return None, None
    hit = len(required & got)
    return hit / len(required), (hit / len(got) if got else 0.0)


def main() -> None:
    retriever = get_retriever()
    rows = []
    for it in items():
        started = time.perf_counter()
        ctx = retriever.retrieve(it["question"], exclude_examples=it["exclude"])
        ms = (time.perf_counter() - started) * 1000
        got = {"tables": set(ctx.tables), "joins": {norm_join(j) for j in ctx.joins}, "metrics": set(ctx.metrics),
               "values": {(v.value, c) for v in ctx.values for c in v.columns}}
        row = {"suite": it["suite"], "id": it["id"], "question": it["question"], "latency_ms": round(ms, 1)}
        missing = []
        for kind in ("tables", "joins", "metrics", "values"):
            req = it[kind]
            if req is None:
                continue
            if kind == "values":                                  # count a value once, whatever column lists it
                found = {v for v in req if v in got["values"]}
                recall = len(found) / len(req) if req else None
                precision = None
            else:
                recall, precision = pr(req, got[kind])
                found = req & got[kind]
            row[f"{kind}_recall"], row[f"{kind}_precision"] = recall, precision
            missing += [f"{kind}: {sorted(map(str, req - found))}" for _ in [0] if req - found]
        row["all_required_hit"] = not missing
        row["missing"] = missing
        rows.append(row)

    lines = [f"# Retrieval evaluation {datetime.now():%Y-%m-%d %H:%M} ({get_settings().data_size} shop)", "",
             "K: up to 6 tables, 4 metrics, 8 filter values, approved joins among retrieved tables. "
             "Regression = verified examples, leave-one-out (own example hidden). No LLM calls.", ""]
    for suite in ("holdout", "regression"):
        part = [r for r in rows if r["suite"] == suite]
        if not part:
            continue
        lines += [f"## {suite.capitalize()} ({len(part)} questions)", "", "| Context | Recall@K | Precision@K |",
                  "|---|---|---|"]
        for kind in ("tables", "metrics", "joins", "values"):
            rec = [r[f"{kind}_recall"] for r in part if r.get(f"{kind}_recall") is not None]
            pre = [r[f"{kind}_precision"] for r in part if r.get(f"{kind}_precision") is not None]
            if rec:
                lines.append(f"| {kind} | {statistics.mean(rec):.0%} (n={len(rec)}) | "
                             f"{statistics.mean(pre):.0%} |" if pre else f"| {kind} | {statistics.mean(rec):.0%} "
                             f"(n={len(rec)}) | - |")
        hit = sum(r["all_required_hit"] for r in part)
        lat = sorted(r["latency_ms"] for r in part)
        lines += ["", f"All required context retrieved: **{hit}/{len(part)} ({hit / len(part):.0%})** · "
                  f"latency median {statistics.median(lat):.0f} ms, max {lat[-1]:.0f} ms", ""]
        misses = [r for r in part if not r["all_required_hit"]]
        if misses:
            lines.append("Failures:")
            lines += [f"- {r['id']} \"{r['question']}\": missing {'; '.join(r['missing'])}" for r in misses]
            lines.append("")
    out = ROOT / "evals" / "reports"
    out.mkdir(parents=True, exist_ok=True)
    stamp = f"retrieval_{datetime.now():%Y-%m-%d_%H%M}"
    (out / f"{stamp}.json").write_text(json.dumps(rows, indent=1, default=str))
    (out / f"{stamp}.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
