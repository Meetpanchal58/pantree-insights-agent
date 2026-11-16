"""What a SQL query uses: tables, approved joins, columns and SQL patterns (parsed with sqlglot, no LLM).

Used to pick reference queries by structure (graph.py), to measure the query library's coverage
(evals/library_coverage.py) and to check that a query computes the metrics it claims.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

import sqlglot
from sqlglot import exp

from .guardrails import _qualify
from .semantic import load_metrics, load_schema


@dataclass
class Usage:
    tables: set[str] = field(default_factory=set)
    joins: set[str] = field(default_factory=set)
    columns: set[str] = field(default_factory=set)
    patterns: set[str] = field(default_factory=set)


@lru_cache(maxsize=1024)
def analyse(sql: str) -> Usage:
    schema = load_schema()
    tree = sqlglot.parse_one(sql, read="duckdb")
    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    qualified, problem = _qualify(tree, schema)
    if problem:
        raise ValueError(problem)
    alias: dict[str, str] = {}
    bases = []
    for t in qualified.find_all(exp.Table):
        name = t.name.lower()
        if name in schema.tables and name not in cte_names:
            alias[t.alias_or_name.lower()] = name
            bases.append(name)
    u = Usage(tables=set(bases))
    for c in qualified.find_all(exp.Column):
        table = alias.get((c.table or "").lower())
        if table and c.name in schema.tables[table].columns:
            u.columns.add(f"{table}.{c.name}")
    approved = {frozenset(s.strip() for s in j.split("=")): j for j in schema.joins}
    for eq in qualified.find_all(exp.EQ):
        a, b = eq.left, eq.right
        if isinstance(a, exp.Column) and isinstance(b, exp.Column):
            ta, tb = alias.get((a.table or "").lower()), alias.get((b.table or "").lower())
            if ta and tb and ta != tb:
                key = frozenset({f"{ta}.{a.name}", f"{tb}.{b.name}"})
                if key in approved:
                    u.joins.add(approved[key])
    for join in qualified.find_all(exp.Join):
        for ident in join.args.get("using") or []:
            for j in schema.joins:
                if all(side.strip().split(".")[1] == ident.name for side in j.split("=")):
                    sides = {side.strip().split(".")[0] for side in j.split("=")}
                    if sides <= set(bases):
                        u.joins.add(j)
    p = u.patterns
    if tree.find(exp.With):
        p.add("cte")
    if len(set(bases)) >= 3:
        p.add("three_plus_tables")
    if len(bases) > len(set(bases)):
        p.add("same_table_twice")
    if any(j.side == "LEFT" for j in tree.find_all(exp.Join)):
        p.add("left_join")
    if tree.find(exp.Having):
        p.add("having")
    if tree.find(exp.Union):
        p.add("union")
    if tree.find(exp.Exists):
        p.add("exists")
    if tree.find(exp.Filter):
        p.add("filter_clause")
    if tree.find(exp.Case):
        p.add("case")
    if tree.find(exp.DateTrunc) or tree.find(exp.TimestampTrunc) or re.search(r"date_trunc", sql, re.I):
        p.add("date_trunc")
    if tree.find(exp.DateDiff) or re.search(r"date_diff", sql, re.I):
        p.add("date_diff")
    if tree.find(exp.Median) or tree.find(exp.PercentileCont) or tree.find(exp.Quantile):
        p.add("median_percentile")
    for w in tree.find_all(exp.Window):
        fn = w.this
        partitioned, ordered = bool(w.args.get("partition_by")), bool(w.args.get("order"))
        if isinstance(fn, (exp.RowNumber,)) or fn.key in ("rank", "denserank"):
            p.add("rank_window" if not partitioned else "top_n_per_group")
        elif isinstance(fn, (exp.Lag, exp.Lead)):
            p.add("lag_lead")
        elif isinstance(fn, exp.Sum) and ordered:
            p.add("running_total")
        elif isinstance(fn, exp.Sum) and not ordered:
            p.add("share_of_total")
        else:
            p.add("other_window")
    for join in tree.find_all(exp.Join):
        on = join.args.get("on")
        if on is not None and (on.find(exp.GTE) or on.find(exp.LT) or on.find(exp.Between) or on.find(exp.LTE)):
            p.add("range_join")
    if re.search(r"\bqualify\b", sql, re.I):
        p.add("qualify")
    return u


def metric_columns(name: str) -> set[str]:
    """Schema columns a metric's definition uses (for checking a query's `metrics:` claim)."""
    m = load_metrics()
    if name in m.metrics:
        sql = f"SELECT {m.metrics[name].numerator}" + (f", {m.metrics[name].denominator}"
                                                        if m.metrics[name].denominator else "")
    else:
        sql = m.custom_metrics[name].sql
    known = {c for t in load_schema().tables.values() for c in t.columns}
    tree = sqlglot.parse_one(sql, read="duckdb")
    # what the metric measures: columns inside aggregations and their FILTER conditions, not grouping columns
    measured = {c.name for node in [*tree.find_all(exp.AggFunc), *tree.find_all(exp.Filter)]
                for c in node.find_all(exp.Column)}
    return measured & known


