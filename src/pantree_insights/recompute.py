"""Canonical recomputation: metric SQL built by code from metrics.yaml, independent of the LLM's SQL.

Used to verify numbers: a reference query (build time) or the agent's answer (runtime) must agree with what
the metric's own definition gives for the same scope (breakdown, filters, period).

- the metric's numerator/denominator and its source's base filter come from metrics.yaml, rewritten from the
  source's aliases (s.net_amount) to full table names (sales.net_amount)
- breakdown and filter columns are joined with code-built paths from the source's table (joins.py), always as
  LEFT JOINs: many-to-one joins then never drop or repeat rows (an inner join to product_reviews would drop
  the 81% of sale lines that have no review)
- refused (None): custom metrics, sources that are subqueries (tickets), and any join that can repeat rows
"""
from __future__ import annotations

from datetime import date, timedelta

import sqlglot
from sqlglot import exp

from .joins import JoinGraph
from .semantic import load_metrics


def _aliases(from_: str) -> dict[str, str] | None:
    """{'s': 'sales', 'p': 'product_master'} for a source's FROM clause; None if it uses a subquery."""
    tree = sqlglot.parse_one(f"SELECT 1 FROM {from_}", read="duckdb")
    if tree.find(exp.Subquery):
        return None
    return {t.alias_or_name.lower(): t.name.lower() for t in tree.find_all(exp.Table)}


def _full_names(expression: str, aliases: dict[str, str]) -> tuple[str, set[str]]:
    """Rewrite alias.column to table.column; also return the tables used."""
    tree = sqlglot.parse_one(f"SELECT {expression}", read="duckdb")
    used = set()
    for col in tree.find_all(exp.Column):
        table = aliases.get((col.table or "").lower())
        if table:
            col.set("table", exp.to_identifier(table))
            used.add(table)
    return tree.selects[0].sql(dialect="duckdb"), used


def _end_exclusive(end: str) -> str:
    return (date.fromisoformat(end[:10]) + timedelta(days=1)).isoformat()


def canonical_sql(metric: str, graph: JoinGraph, by: list[str] | None = None,
                  filters: list[tuple[str, str]] | None = None, period: tuple[str, str] | None = None,
                  time_column: str | None = None) -> str | None:
    """SQL returning one row per breakdown combination with the metric in column `value`; None if the metric
    cannot be recomputed safely. `by` and filter columns are 'table.column'; `period` is inclusive dates."""
    metrics = load_metrics()
    if metric not in metrics.metrics:
        return None
    m = metrics.metrics[metric]
    source = metrics.sources[m.source]
    aliases = _aliases(source.from_)
    if aliases is None:
        return None
    root = next(iter(aliases.values()))                                   # the source's first table
    value, used = _full_names(m.expression(), aliases)
    where = []
    if source.where:
        clause, more = _full_names(source.where, aliases)
        where.append(clause)
        used |= more
    by, filters = list(by or []), list(filters or [])
    for column, val in filters:
        where.append(f"{column} = '{str(val).replace(chr(39), chr(39) * 2)}'")
    time = time_column or _full_names(source.time_column, aliases)[0]
    if period:
        start, end = period
        where += [f"{time} >= TIMESTAMP '{start[:10]}'", f"{time} < TIMESTAMP '{_end_exclusive(end)}'"]
    tables = [t for t in dict.fromkeys([*used, *(c.split(".")[0] for c in by), *(c.split(".")[0] for c, _ in filters),
                                        time.split(".")[0]]) if t != root]
    try:
        steps = graph.path(root, tables)
    except KeyError:
        return None
    if any(step.fans_out for step in steps):
        return None
    joins = "\n".join(f"LEFT JOIN {s.right} ON {s.on}" for s in steps)
    dims = [f"{c} AS {c.split('.')[1]}" for c in by]
    select = ", ".join([*dims, f"{value} AS value"])
    sql = f"SELECT {select}\nFROM {root}" + (f"\n{joins}" if joins else "")
    if where:
        sql += "\nWHERE " + " AND ".join(where)
    if by:
        sql += "\nGROUP BY " + ", ".join(str(i + 1) for i in range(len(by)))
    return sql
