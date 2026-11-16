"""Coverage of the query library (free): which tables, approved joins, metrics, concept columns and SQL patterns
the reference queries (examples.yaml + semantic/library/) use, and what is still missing.

    uv run python evals/library_coverage.py            # prints gaps; exit code 1 while anything is missing
    uv run python evals/library_coverage.py --run      # also runs every query on the configured shop (DATA_SIZE)

Targets: every table and every metric in at least 2 queries, every approved join and every concept column in
at least 1, every SQL pattern in at least 2. Metrics a query claims (its `metrics:` list) are checked: every
column the metric's definition uses must appear in the query.
"""
from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from pantree_insights.semantic import Example, load_concepts, load_examples, load_library, load_metrics, load_schema  # noqa: E402
from pantree_insights.sqlinfo import Usage, analyse, metric_columns  # noqa: E402

TARGETS = {"tables": 2, "joins": 1, "metrics": 2, "columns": 1, "patterns": 2}
# SQL patterns found in the parsed query; "tagged" patterns are declared in the query's tags
TAGGED = {"funnel", "cohort", "period_comparison", "retention", "pareto", "basket", "per_day_rate"}


def check_metrics(e: Example, usage: Usage) -> list[str]:
    names = {c.split(".")[1] for c in usage.columns}
    return [f"{e.id}: claims {m} but does not use {sorted(metric_columns(m) - names)}"
            for m in e.metrics if metric_columns(m) - names]


def coverage(queries: list[Example]) -> tuple[dict[str, Counter], dict[str, list[str]], list[str], dict]:
    schema, concepts, metrics = load_schema(), load_concepts(), load_metrics()
    counts: dict[str, Counter] = defaultdict(Counter)
    problems = []
    for e in queries:
        u = analyse(e.sql)
        problems += check_metrics(e, u)
        for key in ("tables", "joins", "columns", "patterns"):
            counts[key].update(getattr(u, key))
        counts["patterns"].update(t for t in e.tags if t in TAGGED)
        counts["metrics"].update(e.metrics)
    universe = {
        "tables": list(schema.tables), "joins": list(schema.joins),
        "metrics": list(metrics.metrics) + list(metrics.custom_metrics),
        "columns": sorted({en.column for es in concepts.concepts.values() for en in es.values()}),
        "patterns": ["cte", "three_plus_tables", "same_table_twice", "left_join", "having", "union", "exists",
                     "filter_clause", "case", "date_trunc", "date_diff", "median_percentile", "rank_window",
                     "top_n_per_group", "lag_lead", "running_total", "share_of_total", "range_join", *sorted(TAGGED)],
    }
    gaps = {k: [x for x in universe[k] if counts[k][x] < TARGETS[k]] for k in universe}
    return counts, gaps, problems, universe


def run_checks(library: list[Example]) -> list[str]:
    """Every library query on the configured shop: runs, has rows, no duplicate rows, and does not contain the
    answer to a scored question (holdout gold or verified example)."""
    import yaml

    from pantree_insights.db import connect, run_query
    from pantree_insights.guardrails import check_sql

    con = connect()

    def rows(sql: str) -> frozenset:
        frame = run_query(con, check_sql(sql).sql).frame
        norm = lambda v: round(v, 4) if isinstance(v, float) else str(v).lower()          # noqa: E731
        return frozenset(frozenset(norm(v) for v in r) for r in frame.itertuples(index=False))

    holdout = yaml.safe_load((ROOT / "evals" / "holdout.yaml").read_text())["questions"]
    scored = {q["id"]: q["gold_sql"] for q in holdout if q.get("gold_sql")} | \
             {e.id: e.sql for e in load_examples().examples}
    gold = {k: rows(sql) for k, sql in scored.items()}
    gold = {k: fp for k, fp in gold.items() if not (len(fp) == 1 and next(iter(fp)) <= {0, "0", "none", "nan"})}
    problems = []
    for e in library:
        frame = run_query(con, check_sql(e.sql).sql).frame
        if frame.empty or frame.duplicated().any():
            problems.append(f"{e.id}: {'no rows' if frame.empty else 'duplicate rows'}")
        mine = rows(e.sql)
        leaked = [k for k, fp in gold.items() if fp and all(any(g <= m for m in mine) for g in fp)]
        if leaked:
            problems.append(f"{e.id}: contains the answer to {leaked}")
    return problems


def main() -> None:
    library = load_library()
    queries = load_examples().examples + library
    counts, gaps, problems, universe = coverage(queries)
    by_area = Counter(e.area for e in library)
    print(f"{len(load_examples().examples)} verified examples + {len(library)} library queries "
          f"({', '.join(f'{a} {n}' for a, n in sorted(by_area.items()))})\n")
    for key, missing in gaps.items():
        print(f"{key}: {len(universe[key]) - len(missing)}/{len(universe[key])} at target {TARGETS[key]}"
              + (f"; missing: {missing}" if missing else ""))
    if "--run" in sys.argv:
        problems += run_checks(library)
        print(f"ran {len(library)} queries on the configured shop")
    for p in problems:
        print("PROBLEM", p)
    sys.exit(1 if any(gaps.values()) or problems else 0)


if __name__ == "__main__":
    main()
