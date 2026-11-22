"""Spec: validation."""
from __future__ import annotations

import math
import re

import sqlglot
from sqlglot import exp

from .models import TICKETS, Catalog, Filter, QuerySpec, SpecError


def _tables(expression: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"\b([a-z_]+)\.[a-z_]+\b", expression)))


def _literal(value) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def _filtered_metric(expression: str, predicate: str) -> str:
    """Apply a population to every aggregate, including both sides of a named ratio."""
    tree = sqlglot.parse_one(f"SELECT {expression}", read="duckdb")
    condition = sqlglot.parse_one(predicate, read="duckdb")
    for aggregate in list(tree.find_all(exp.AggFunc)):
        parent = aggregate.parent
        if isinstance(parent, exp.Filter):
            parent.set("expression", exp.Where(this=exp.and_(parent.expression.this.copy(), condition.copy())))
        else:
            aggregate.replace(exp.Filter(this=aggregate.copy(), expression=exp.Where(this=condition.copy())))
    return tree.selects[0].sql(dialect="duckdb")


def validate(spec: QuerySpec, cat: Catalog) -> list[str]:
    """Problems, phrased for the user; [] means the spec can be compiled as asked."""
    from .compiler import _default_time, compile_multifact, compile_spec
    problems: list[str] = []
    if spec.operation:
        base = spec.model_copy(update={"operation": None, "shares": []})
        problems = validate(base, cat)
        if not problems:
            try:
                compile_spec(spec, cat)
            except SpecError as error:
                problems.append(str(error))
        return problems
    table = spec.table
    if table != TICKETS and table not in cat.schema.tables:
        return [f"unknown table {table}"]
    names = [m.name for m in spec.measures]
    if any(not re.fullmatch(r"[a-z][a-z0-9_]*", n) for n in names):
        problems.append("measure names must be lowercase SQL identifiers")
    multi = len({cat.source_table(m.metric) for m in spec.measures if m.metric in cat.metrics.metrics}) > 1
    if multi:
        try:
            compile_multifact(spec, cat)
        except (SpecError, KeyError, ValueError) as error:
            problems.append(str(error))
        return problems
    if len(set(names)) != len(names):
        problems.append("two measures have the same name")
    if not spec.measures and not spec.columns:
        problems.append("nothing to compute: no measure and no columns to list")
    if spec.columns and spec.measures:
        problems.append("a spec either lists rows (columns) or computes measures, not both")
    for m in spec.measures:
        if m.metric:
            if m.metric not in cat.metrics.metrics:
                problems.append(f"unknown metric {m.metric}")
            elif cat.source_table(m.metric) != table:
                problems.append(f"{m.metric} is measured on {cat.source_table(m.metric)}, not {table}")
        if m.agg and m.agg not in ("count", "share") and not m.field:
            problems.append(f"{m.name}: {m.agg} needs a field")
        if m.agg in ("sum", "avg", "min", "max") and m.field:
            problems += _check_field(m.field, table, cat, numeric=True)
        if m.agg == "share" and not m.where:
            problems.append(f"{m.name}: a share needs the condition (where) it counts")
        if m.ratio and (len(m.ratio) != 2 or any(r not in [x.name for x in spec.measures if not x.ratio]
                                                for r in m.ratio)):
            problems.append(f"{m.name}: ratio must name two non-ratio measures")
        if m.ratio and m.where:
            problems.append(f"{m.name}: apply filters to its numerator and denominator measures")
        for f in m.where:
            problems += _check_filter(f, table, cat)
    for d in spec.dimensions:
        problems += _check_field(d, table, cat)
    for f in spec.filters:
        problems += _check_filter(f, table, cat)
    for c in spec.columns:
        problems += _check_field(c, table, cat)
    if spec.time:
        if spec.time.start and spec.time.end and spec.time.start > spec.time.end:
            problems.append("the period starts after it ends")
        if spec.time.field:
            problems += _check_field(spec.time.field, table, cat)
            if not problems and cat.kind(cat.resolve(spec.time.field, table)[0], table) != "timestamp":
                problems.append("the time field must be a date or timestamp")
        elif _default_time(spec, cat) is None and (spec.time.start or spec.time.end or spec.time.grain):
            problems.append(f"{table} has no main date: say which date the period applies to")
    if spec.ranking and spec.ranking.by not in names:
        problems.append(f"ranking by {spec.ranking.by}, which is not a measure")
    for s in spec.shares:
        if s not in names:
            problems.append(f"share of total for {s}, which is not a measure")
        else:
            measure = next(m for m in spec.measures if m.name == s)
            if (measure.ratio or measure.agg in ("avg", "min", "max", "count_distinct", "share")
                    or measure.metric in cat.metrics.metrics
                    and cat.metrics.metrics[measure.metric].time_aggregation != "additive"):
                problems.append(f"{s}: share of total needs an additive numerator and a disjoint population")
    if not problems:
        try:
            compile_spec(spec, cat)
        except SpecError as error:
            problems.append(str(error))
    return problems


def _check_field(field: str, table: str, cat: Catalog, numeric: bool = False) -> list[str]:
    try:
        expression, _, _ = cat.resolve(field, table)
    except SpecError as error:
        return [str(error)]
    if numeric and cat.kind(expression, table) not in ("number", "integer"):
        return [f"{field} is not a number"]
    return []


def _check_filter(f: Filter, table: str, cat: Catalog) -> list[str]:
    try:
        expression, _, entry = cat.resolve(f.field, table)
    except SpecError as error:
        return [str(error)]
    if f.op in ("is_set", "is_not_set"):
        return []
    if f.op.endswith("_field"):
        return _check_field(f.other, table, cat) if f.other else [f"{f.field}: {f.op} needs the other column"]
    if not f.values:
        return [f"filter on {f.field} has no value"]
    if any(isinstance(v, float) and not math.isfinite(v) for v in f.values):
        return [f"filter on {f.field} must use finite values"]
    if f.op in ("lt", "lte", "gt", "gte"):
        ok = len(f.values) == 1 and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                       and math.isfinite(v) for v in f.values)
        return [] if ok else [f"{f.field} {f.op} needs a number"]
    allowed = entry.labels() if entry is not None and entry.expression else cat.values_of(expression)
    if allowed is not None:
        unknown = [v for v in f.values if not isinstance(v, bool) and str(v) not in allowed]
        if unknown:
            close = ", ".join(f"'{a}'" for a in allowed[:8])
            return [f"{f.field} has no value {', '.join(map(repr, unknown))}; stored values include {close}"]
    elif cat.con is not None and cat.kind(expression, table) == "text":     # many values: check they exist
        t, c = expression.split(".")
        cursor = cat.con.cursor()
        values = list(dict.fromkeys(str(v) for v in f.values))
        try:
            placeholders = ", ".join("?" for _ in values)
            found = {str(row[0]) for row in cursor.execute(
                f'SELECT DISTINCT "{c}" FROM {t} WHERE "{c}" IN ({placeholders})', values).fetchall()}
            missing = [v for v in values if v not in found]
            if missing:
                return [f"{f.field} has no value '{missing[0]}'"]
        finally:
            cursor.close()
    return []
