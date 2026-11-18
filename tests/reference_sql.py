"""Test-only reference: an independent, older implementation of 'metric split by columns' (from metrics.yaml).

tests/test_spec.py compares the production compiler (src/pantree_insights/spec.py) with it for every metric and
dimension: two implementations written separately must give the same numbers.
"""
from __future__ import annotations

from datetime import date, timedelta

import sqlglot
from sqlglot import exp

from pantree_insights.joins import JoinGraph
from pantree_insights.semantic import load_metrics
from pantree_insights.spec import _aliases, _full_names


def _end_exclusive(end: str) -> str:
    return (date.fromisoformat(end[:10]) + timedelta(days=1)).isoformat()


def _tables_of(expression: str) -> list[str]:
    tree = sqlglot.parse_one(f"SELECT {expression}", read="duckdb")
    return [c.table.lower() for c in tree.find_all(exp.Column) if c.table]


def canonical_sql(metric: str, graph: JoinGraph, by: list[str] | dict[str, str] | None = None,
                  filters: list[tuple[str, str]] | None = None, period: tuple[str, str] | None = None,
                  time_column: str | None = None, conditions: list[str] | None = None) -> str | None:
    """SQL returning one row per breakdown combination with the metric in column `value`; None if the metric
    cannot be recomputed safely. `by`: 'table.column' list, or {result name: SQL expression with full table
    names} (e.g. {"month": "date_trunc('month', sales.placed_at)"}); filters are 'table.column' = value;
    `conditions` are extra SQL conditions with full table names; `period` is inclusive dates."""
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
    dims_map = dict(by) if isinstance(by, dict) else {c.split(".")[1]: c for c in (by or [])}
    filters, conditions = list(filters or []), list(conditions or [])
    where += conditions
    grouped: dict[str, list[str]] = {}
    for column, val in filters:
        grouped.setdefault(column, []).append(str(val).replace("'", "''"))
    for column, values in grouped.items():
        values = list(dict.fromkeys(values))
        literals = ", ".join(f"'{v}'" for v in values)
        where.append(f"{column} = {literals}" if len(values) == 1 else f"{column} IN ({literals})")
    default_time = _full_names(source.time_column, aliases)[0]
    time = time_column or default_time
    # A different milestone selects events that actually happened, even without date bounds.
    # Otherwise "shipped, all data" would include orders that have never shipped.
    if time != default_time:
        where.append(f"{time} IS NOT NULL")
    if period:
        start, end = period
        where += [f"{time} >= TIMESTAMP '{start[:10]}'", f"{time} < TIMESTAMP '{_end_exclusive(end)}'"]
    needed = [*used, *(t for e in dims_map.values() for t in _tables_of(e)), *(c.split(".")[0] for c, _ in filters),
              *(t for c in conditions for t in _tables_of(c)), time.split(".")[0]]
    tables = [t for t in dict.fromkeys(needed) if t != root]
    try:
        steps = graph.path(root, tables)
    except KeyError:
        return None
    if any(step.fans_out for step in steps):
        return None
    joins = "\n".join(f"LEFT JOIN {s.right} ON {s.on}" for s in steps)
    dims = [f"{e} AS {name}" for name, e in dims_map.items()]
    select = ", ".join([*dims, f"{value} AS value"])
    sql = f"SELECT {select}\nFROM {root}" + (f"\n{joins}" if joins else "")
    if where:
        sql += "\nWHERE " + " AND ".join(where)
    if dims_map:
        sql += "\nGROUP BY " + ", ".join(str(i + 1) for i in range(len(dims_map)))
    return sql


def same_numbers(result, canonical, key: str, column: str, tolerance: float = 0.01) -> list[str]:
    """Problems where result[column] differs from canonical['value'] per key (empty keys ignored)."""
    import pandas as pd

    def table(frame, col):
        return {str(k): v for k, v in zip(frame[key], frame[col], strict=True) if not pd.isna(k)}
    mine, ref = table(result, column), table(canonical, "value")
    problems = [f"{k}: missing" for k in set(ref) - set(mine)] + [f"{k}: extra" for k in set(mine) - set(ref)]
    for k in set(mine) & set(ref):
        a, b = mine[k], ref[k]
        if pd.isna(a) != pd.isna(b) or (not pd.isna(a) and abs(float(a) - float(b)) > tolerance * max(abs(b), 1e-9)):
            problems.append(f"{k}: {a} vs {b}")
    return problems
