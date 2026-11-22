"""Bounded second-stage analytics over the existing grain-aware compiler.

Period comparisons recompute both populations, including distinct counts and ratio denominators.
Windows may sum only explicitly time-additive metrics; missing days produce no output row.
"""
from __future__ import annotations

from datetime import timedelta

import sqlglot

from .spec import Catalog, QuerySpec, SpecError, compile_spec


def compile_operation(spec: QuerySpec, cat: Catalog) -> str:
    op = spec.operation
    if spec.columns or spec.limit or spec.ranking or not spec.measures:
        raise SpecError("analytical operations require measures without lists or early ranking")
    if not spec.time or not spec.time.start or not spec.time.end:
        raise SpecError("analytical operations need explicit start and end dates")
    if (spec.time.end - spec.time.start).days > 366:
        raise SpecError("analytical operations are bounded to 367 output days")
    base = spec.model_copy(update={"operation": None, "shares": []})
    names = [m.name for m in spec.measures]
    for name in spec.shares:
        m = next((m for m in spec.measures if m.name == name), None)
        if (op.kind != "period_compare" or m is None or not m.metric
                or cat.metrics.metrics[m.metric].time_aggregation != "additive"):
            raise SpecError("contributions require a named additive metric in a period comparison")

    def query(s):
        tree = sqlglot.parse_one(compile_spec(s, cat), read="duckdb")
        tree.set("order", None)
        return tree

    tree = query(base)
    keys = [e.alias_or_name for e in tree.selects if e.alias_or_name not in names]
    if set(keys) & set(names) or len({e.alias_or_name for e in tree.selects}) != len(tree.selects):
        raise SpecError("analytical output columns must have unique names")
    suffixes = ("previous", "delta", "change_ratio") if op.kind == "period_compare" else (op.kind,)
    generated = {f"{n}_{suffix}" for n in names for suffix in suffixes}
    generated.update(f"{n}_contribution" for n in spec.shares)
    if generated & (set(names) | set(keys)):
        raise SpecError("measure name collides with an analytical output")
    if op.kind == "period_compare":
        if spec.time.grain:
            raise SpecError("period_compare compares whole periods; remove the time grain")
        if not op.previous_start or not op.previous_end or op.previous_start > op.previous_end:
            raise SpecError("period_compare needs an ordered previous_start and previous_end")
        if (op.previous_end - op.previous_start).days > 366:
            raise SpecError("comparison periods are bounded to 367 days")
        previous = query(base.model_copy(update={"time": base.time.model_copy(update={
            "start": op.previous_start, "end": op.previous_end})}))
        columns = [f'COALESCE(c."{k}", p."{k}") AS "{k}"' for k in keys]
        for n in names:
            columns += [f'c."{n}"', f'p."{n}" AS "{n}_previous"',
                        f'c."{n}" - p."{n}" AS "{n}_delta"',
                        f'c."{n}" / NULLIF(p."{n}", 0) - 1 AS "{n}_change_ratio"']
            if n in spec.shares:
                delta = f'(c."{n}" - p."{n}")'
                columns.append(f'CASE WHEN COUNT(*) FILTER (WHERE {delta} IS NULL) OVER () = 0 '
                               f'THEN {delta} / NULLIF(SUM({delta}) OVER (), 0) END AS "{n}_contribution"')
        on = " AND ".join(f'c."{k}" IS NOT DISTINCT FROM p."{k}"' for k in keys) or "TRUE"
        return (f"WITH current_period AS ({tree.sql(dialect='duckdb')}), "
                f"previous_period AS ({previous.sql(dialect='duckdb')}) "
                f"SELECT {', '.join(columns)} FROM current_period c FULL OUTER JOIN previous_period p ON {on}")
    if op.previous_start or op.previous_end:
        raise SpecError("previous dates are only valid for period_compare")
    if spec.time.grain != "day":
        raise SpecError("windows require a daily grain; output contains observed days only")
    if any(not m.metric or cat.metrics.metrics[m.metric].time_aggregation != "additive" for m in spec.measures):
        raise SpecError("windows require explicitly time-additive metrics; ratios, distinct counts and snapshots "
                        "must be recomputed over their population")
    # Read the lookback before filtering output. A seven-day window means calendar days, not seven observed rows.
    if op.kind == "rolling_sum":
        base = base.model_copy(update={"time": base.time.model_copy(update={
            "start": base.time.start - timedelta(days=op.days - 1)})})
    tree = query(base)
    partition = [k for k in keys if k != "day"]
    window = ("PARTITION BY " + ", ".join(f'"{k}"' for k in partition) + " ") if partition else ""
    frame = (f"RANGE BETWEEN INTERVAL {op.days - 1} DAY PRECEDING AND CURRENT ROW"
             if op.kind == "rolling_sum" else "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW")
    columns = [f'SUM("{n}") OVER ({window}ORDER BY day {frame}) AS "{n}_{op.kind}"' for n in names]
    return (f"WITH daily_values AS ({tree.sql(dialect='duckdb')}), "
            f"window_values AS (SELECT {', '.join(keys + names)}, {', '.join(columns)} FROM daily_values) "
            f"SELECT {', '.join(keys + names + sorted(generated))} FROM window_values "
            f"WHERE day >= DATE '{spec.time.start}' ORDER BY day")
