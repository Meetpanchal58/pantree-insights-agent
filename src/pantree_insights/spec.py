"""v2 core: a question as a structured QuerySpec, checked against the catalog and compiled to SQL by code.

The model's only job (phase 2) is to fill a QuerySpec. Every name in it is a closed choice: a metric from
metrics.yaml, a concept from concepts.yaml, or a column from the catalog. Code then
- validates it (`validate`): every metric, field, value, operator and date must exist and fit; problems come back
  as questions for the user, never as guesses;
- compiles it (`compile_spec`): one SQL statement from the metric definitions, the concept map and the join graph.

No model writes SQL on this path, so a verified answer is as correct as the compiler and the catalog, both of which
are tested against hand-written SQL (tests/test_spec.py, evals/gold_specs.yaml).

Measures
- `metric`: a named metric (metrics.yaml), e.g. realized_revenue, return_rate
- `agg` over a catalog column: count | count_distinct | sum | avg | min | max, optionally with `where` (a FILTER)
- `agg: share`: share of rows meeting `where` (escalated share, rejected share)
- `ratio`: one measure divided by another (units on hand / capacity)
Fields are concept names ("category", "payment_type", "city") or qualified catalog columns ("sellers.region").
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from functools import lru_cache
from typing import Literal

import sqlglot
from pydantic import BaseModel, Field, model_validator
from sqlglot import exp

from .joins import JoinGraph
from .semantic import ConceptEntry, Concepts, Metrics, Schema

GRAINS = ("day", "week", "month", "quarter", "year")
TICKETS = "tickets"                          # virtual table: the tickets source (one row per support ticket)


def _aliases(from_: str) -> dict[str, str] | None:
    """{'s': 'sales', 'p': 'product_master'} for a metric source's FROM clause; None if it is a subquery."""
    tree = sqlglot.parse_one(f"SELECT 1 FROM {from_}", read="duckdb")
    if tree.find(exp.Subquery):
        return None
    return {t.alias_or_name.lower(): t.name.lower() for t in tree.find_all(exp.Table)}


def _full_names(expression: str, aliases: dict[str, str]) -> tuple[str, set[str]]:
    """Rewrite alias.column to table.column (s.net_amount -> sales.net_amount); also return the tables used."""
    tree = sqlglot.parse_one(f"SELECT {expression}", read="duckdb")
    used = set()
    for col in tree.find_all(exp.Column):
        table = aliases.get((col.table or "").lower())
        if table:
            col.set("table", exp.to_identifier(table))
            used.add(table)
    return tree.selects[0].sql(dialect="duckdb"), used


# ----------------------------------------------------------------------------- the spec
class Filter(BaseModel):
    field: str = Field(description="Concept name or table.column from the catalog")
    op: Literal["eq", "in", "not_in", "lt", "lte", "gt", "gte", "is_set", "is_not_set",
                "lte_field", "gte_field", "lt_field", "gt_field"] = "in"
    values: list[str | float | bool] = Field(default_factory=list, description="Stored values exactly as listed")
    other: str | None = Field(None, description="For *_field ops: the column compared with")


class Measure(BaseModel):
    name: str = Field(description="Result column name")
    metric: str | None = Field(None, description="Named metric from metrics.yaml")
    agg: Literal["count", "count_distinct", "sum", "avg", "min", "max", "share"] | None = None
    field: str | None = Field(None, description="Column the aggregate reads (not needed for count and share)")
    where: list[Filter] = Field(default_factory=list, description="Rows the measure counts (a FILTER clause)")
    ratio: list[str] | None = Field(None, description="[numerator measure, denominator measure] by name")

    @model_validator(mode="after")
    def _one_kind(self) -> Measure:
        kinds = [self.metric is not None, self.agg is not None, self.ratio is not None]
        if sum(kinds) != 1:
            raise ValueError(f"measure {self.name}: give exactly one of metric, agg or ratio")
        return self


class TimeSpec(BaseModel):
    field: str | None = Field(None, description="Date column (default: the table's main date)")
    start: date | None = None                 # inclusive
    end: date | None = None                   # inclusive
    grain: Literal["day", "week", "month", "quarter", "year"] | None = None


class Ranking(BaseModel):
    by: str = Field(description="Measure name to rank by")
    order: Literal["desc", "asc"] = "desc"
    limit: int = Field(10, ge=1, le=500)


class QuerySpec(BaseModel):
    table: str = Field(description="What one row is: the table whose rows are counted (sales, customers, ...)")
    measures: list[Measure] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list, description="Splits: concepts or table.column")
    filters: list[Filter] = Field(default_factory=list)
    time: TimeSpec | None = None
    ranking: Ranking | None = None
    shares: list[str] = Field(default_factory=list, description="Measures also shown as share of the total")
    columns: list[str] = Field(default_factory=list, description="List mode: columns of the rows to list")
    limit: int | None = Field(None, ge=1, le=100, description="List mode: at most this many rows")


# ----------------------------------------------------------------------------- the catalog the spec is checked against
class Catalog:
    """Everything a spec may name, with how each name becomes SQL."""

    def __init__(self, schema: Schema, metrics: Metrics, concepts: Concepts, graph: JoinGraph,
                 catalog: dict, labels: dict[str, str] | None = None, con=None):
        self.schema, self.metrics, self.concepts, self.graph = schema, metrics, concepts, graph
        self.catalog, self.labels, self.con = catalog, labels or {}, con
        self.subject_of = {s.table: name for name, s in concepts.subjects.items()}

    # tables and sources
    def source_table(self, metric: str) -> str:
        source = self.metrics.sources[self.metrics.metrics[metric].source]
        aliases = _aliases(source.from_)
        return TICKETS if aliases is None else next(iter(aliases.values()))

    def tickets_sql(self) -> str:
        source = self.metrics.sources["tickets"]
        return re.sub(r"\)\s*tk\s*$", ")", source.from_.strip())

    def table_columns(self, table: str) -> dict[str, str]:
        if table == TICKETS:
            return {"ticket_id": "text", "opened_at": "timestamp", "channel": "text", "issue_category": "text",
                    "resolution_status": "text", "contacts": "integer"}
        return {c: d.split(" - ")[0].strip() for c, d in self.schema.tables[table].columns.items()}

    # fields
    def resolve(self, field: str, table: str) -> tuple[str, str, ConceptEntry | None]:
        """field -> (SQL expression with full table names, result name, concept entry or None)."""
        if "." in field:
            t, c = field.split(".", 1)
            if table == TICKETS and t == "support_contacts" and c in self.table_columns(TICKETS):
                t = TICKETS
            if t not in self.schema.tables and t != TICKETS:
                raise SpecError(f"unknown table {t}")
            if c not in self.table_columns(t):
                raise SpecError(f"unknown column {field}")
            return f"{t}.{c}", c, None
        subject = self.subject_of.get("support_contacts" if table == TICKETS else table)
        entry = self.concepts.column(field, subject) if subject else None
        if entry is None:
            raise SpecError(f"'{field}' is not a cut of {table}; use a concept listed for it or table.column")
        if table == TICKETS:
            col = entry.column.split(".")[1]
            if col not in self.table_columns(TICKETS):
                raise SpecError(f"'{field}' is not known per ticket")
            return f"{TICKETS}.{col}", field, entry
        return entry.sql(), field, entry

    def kind(self, expression: str, table: str) -> str:
        m = re.fullmatch(r"(\w+)\.(\w+)", expression)
        return self.table_columns(m.group(1)).get(m.group(2), "text") if m else "text"

    def values_of(self, expression: str) -> list[str] | None:
        m = re.fullmatch(r"(\w+)\.(\w+)", expression)
        if not m or m.group(1) not in self.catalog:
            return None
        return self.catalog[m.group(1)]["columns"].get(m.group(2), {}).get("values")


class SpecError(ValueError):
    """A spec that cannot be compiled; the message is phrased for the user."""


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


# ----------------------------------------------------------------------------- validation
def validate(spec: QuerySpec, cat: Catalog) -> list[str]:
    """Problems, phrased for the user; [] means the spec can be compiled as asked."""
    problems: list[str] = []
    table = spec.table
    if table != TICKETS and table not in cat.schema.tables:
        return [f"unknown table {table}"]
    names = [m.name for m in spec.measures]
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
        if m.ratio and (len(m.ratio) != 2 or any(r not in names for r in m.ratio)):
            problems.append(f"{m.name}: ratio must name two other measures")
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
        elif _default_time(spec, cat) is None and (spec.time.start or spec.time.end or spec.time.grain):
            problems.append(f"{table} has no main date: say which date the period applies to")
    if spec.ranking and spec.ranking.by not in names:
        problems.append(f"ranking by {spec.ranking.by}, which is not a measure")
    for s in spec.shares:
        if s not in names:
            problems.append(f"share of total for {s}, which is not a measure")
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
        return [] if f.other else [f"{f.field}: {f.op} needs the other column"]
    if not f.values:
        return [f"filter on {f.field} has no value"]
    if f.op in ("lt", "lte", "gt", "gte"):
        ok = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in f.values)
        return [] if ok else [f"{f.field} {f.op} needs a number"]
    allowed = entry.labels() if entry is not None and entry.expression else cat.values_of(expression)
    if allowed is not None:
        unknown = [v for v in f.values if not isinstance(v, bool) and str(v) not in allowed]
        if unknown:
            close = ", ".join(f"'{a}'" for a in allowed[:8])
            return [f"{f.field} has no value {', '.join(map(repr, unknown))}; stored values include {close}"]
    elif cat.con is not None and cat.kind(expression, table) == "text":     # many values: check they exist
        for v in f.values:
            t, c = expression.split(".")
            if cat.con.execute(f'SELECT 1 FROM {t} WHERE "{c}" = ? LIMIT 1', [str(v)]).fetchone() is None:
                return [f"{f.field} has no value '{v}'"]
    return []


# ----------------------------------------------------------------------------- compilation
def _default_time(spec: QuerySpec, cat: Catalog) -> str | None:
    metric = next((m.metric for m in spec.measures if m.metric), None)
    if metric:
        source = cat.metrics.sources[cat.metrics.metrics[metric].source]
        aliases = _aliases(source.from_) or {"tk": TICKETS}
        return _full_names(source.time_column, aliases)[0]
    subject = cat.subject_of.get(spec.table)
    return cat.concepts.subjects[subject].time if subject else None


def compile_spec(spec: QuerySpec, cat: Catalog) -> str:
    """One DuckDB SELECT for the spec. Raises SpecError if it cannot be built safely."""
    table = spec.table
    where: list[str] = []
    needed: list[str] = []                     # tables the query reads besides the base table
    own_joins: dict[str, str] = {}
    projected = [*spec.dimensions, *spec.columns, *(m.field for m in spec.measures if m.field)]
    if spec.time and spec.time.field:
        projected.append(spec.time.field)
    projection_tables = {t for field in projected for t in _tables(cat.resolve(field, table)[0])}

    def field_sql(field: str) -> tuple[str, str]:
        expression, name, entry = cat.resolve(field, table)
        if entry is not None and entry.join:
            own_joins[entry.join.split()[0]] = entry.join
        needed.extend(t for t in _tables(expression) if t != table)
        return expression, name

    def condition(f: Filter, register: bool = True) -> str:
        expression = field_sql(f.field)[0] if register else cat.resolve(f.field, table)[0]
        if f.op == "is_set":
            return f"{expression} IS NOT NULL"
        if f.op == "is_not_set":
            return f"{expression} IS NULL"
        if f.op.endswith("_field"):
            other = field_sql(f.other)[0] if register else cat.resolve(f.other, table)[0]
            op = {"lte_field": "<=", "gte_field": ">=", "lt_field": "<", "gt_field": ">"}[f.op]
            return f"{expression} {op} {other}"
        if f.op in ("lt", "lte", "gt", "gte"):
            op = {"lt": "<", "lte": "<=", "gt": ">", "gte": ">="}[f.op]
            return f"{expression} {op} {_literal(f.values[0])}"
        values = ", ".join(_literal(v) for v in f.values)
        if f.op == "not_in":
            return f"{expression} NOT IN ({values})"
        return f"{expression} = {values}" if len(f.values) == 1 else f"{expression} IN ({values})"

    def conditions(filters: list[Filter]) -> list[str]:
        # A date-window filter selects membership. Joining all matching events would count
        # a sale repeatedly when events overlap. Keep event breakdowns as explicit joins.
        memberships: dict[str, list[str]] = {}
        clauses = []
        joins = {}
        for f in filters:
            _, _, entry = cat.resolve(f.field, table)
            if entry is not None and entry.join:
                joins[entry.join.split()[0]] = entry.join
        # Cross-field predicates retain the existing explicit-join path.
        joined = projection_tables | {t for f in filters if f.op.endswith("_field")
                                      for t in _tables(cat.resolve(f.field, table)[0])}
        for f in filters:
            expression, _, _ = cat.resolve(f.field, table)
            targets = _tables(expression)
            join = joins.get(targets[0]) if len(targets) == 1 else None
            if join and targets[0] not in joined:
                memberships.setdefault(join, []).append(condition(f, register=False))
            else:
                clauses.append(condition(f))
        for join, predicates in memberships.items():
            target, on = join.split(" ON ", 1)
            clauses.append(f"EXISTS (SELECT 1 FROM {target} WHERE ({on}) AND "
                           + " AND ".join(f"({p})" for p in predicates) + ")")
        return clauses

    # measures
    sources = {cat.metrics.metrics[m.metric].source for m in spec.measures if m.metric}
    selects: dict[str, str] = {}
    for m in spec.measures:
        if m.metric:
            metric = cat.metrics.metrics[m.metric]
            source = cat.metrics.sources[metric.source]
            aliases = _aliases(source.from_) or {"tk": TICKETS}
            expression, used = _full_names(metric.expression(), aliases)
            needed.extend(t for t in used if t != table)
            cond = " AND ".join(conditions(m.where))
            selects[m.name] = _filtered_metric(expression, cond) if cond else expression
        elif m.agg:
            cond = " AND ".join(conditions(m.where))
            target = field_sql(m.field)[0] if m.field else None
            if m.agg == "share":
                selects[m.name] = (f"COUNT(*) FILTER (WHERE {cond}) * 1.0 / NULLIF(COUNT(*), 0)")
            else:
                inner = {"count": "*", "count_distinct": f"DISTINCT {target}"}.get(m.agg, target)
                fn = {"count_distinct": "COUNT", "count": "COUNT"}.get(m.agg, m.agg.upper())
                selects[m.name] = f"{fn}({inner})" + (f" FILTER (WHERE {cond})" if cond else "")
    for m in spec.measures:
        if m.ratio:
            if m.where:
                raise SpecError(f"{m.name}: apply filters to its numerator and denominator measures")
            a, b = m.ratio
            selects[m.name] = f"({selects[a]}) * 1.0 / NULLIF(({selects[b]}), 0)"

    # filters, period and the table's own rules
    for name in sources:
        source = cat.metrics.sources[name]
        if source.where:
            aliases = _aliases(source.from_) or {"tk": TICKETS}
            clause, used = _full_names(source.where, aliases)
            needed.extend(t for t in used if t != table)
            where.append(clause)
    subject = cat.subject_of.get(table)
    if subject and cat.concepts.subjects[subject].where and cat.concepts.subjects[subject].where not in where:
        where.append(cat.concepts.subjects[subject].where)
    where.extend(conditions(spec.filters))
    time_sql = None
    if spec.time:
        default = _default_time(spec, cat)
        time_sql = field_sql(spec.time.field)[0] if spec.time.field else default
        if time_sql is None:
            raise SpecError(f"{table} has no main date")
        if spec.time.field and time_sql != default:     # another milestone: only rows where it happened
            where.append(f"{time_sql} IS NOT NULL")
        if spec.time.start:
            where.append(f"{time_sql} >= TIMESTAMP '{spec.time.start.isoformat()}'")
        if spec.time.end:
            where.append(f"{time_sql} < TIMESTAMP '{(spec.time.end + timedelta(days=1)).isoformat()}'")

    # splits (a name several IDs share is grouped with its ID)
    groups: list[tuple[str, str]] = []
    if spec.time and spec.time.grain:
        groups.append((spec.time.grain, f"date_trunc('{spec.time.grain}', {time_sql})"))
    for d in spec.dimensions:
        expression, name = field_sql(d)
        id_column = cat.labels.get(expression)
        if id_column:
            groups.append((id_column, f"{expression.split('.')[0]}.{id_column}"))
        groups.append((name, expression))
    list_cols = []
    for c in spec.columns:
        expression, name = field_sql(c)
        list_cols.append((name, expression))

    # joins: approved key joins from the base table, LEFT so no base row is lost; own joins (event windows)
    keyed = [t for t in dict.fromkeys(needed) if t not in own_joins and t != table]
    base = f"({cat.tickets_sql()}) AS {TICKETS}" if table == TICKETS else table
    try:
        steps = cat.graph.path(table, keyed) if keyed and table != TICKETS else []
    except KeyError as error:
        raise SpecError(str(error)) from error
    if keyed and table == TICKETS:
        raise SpecError("per-ticket questions can only use the ticket's own channel, issue and status")
    if any(s.fans_out for s in steps) and spec.measures:
        bad = next(s for s in steps if s.fans_out)
        raise SpecError(f"joining {bad.right} would repeat {bad.left} rows and inflate the numbers")
    joins = [f"LEFT JOIN {s.right} ON {s.on}" for s in steps] + [f"JOIN {j}" for j in own_joins.values()]

    # assemble
    if list_cols:
        select = ", ".join(f"{e} AS {n}" for n, e in list_cols)
        sql = f"SELECT {select}\nFROM {base}" + "".join(f"\n{j}" for j in joins)
        sql += ("\nWHERE " + " AND ".join(where)) if where else ""
        sql += "\nORDER BY " + ", ".join(str(i + 1) for i in range(len(list_cols)))
        return sql + f"\nLIMIT {spec.limit or 100}"
    cols = [f"{e} AS {n}" for n, e in groups] + [f"{e} AS {n}" for n, e in selects.items()]
    partition = f"PARTITION BY q.{spec.time.grain}" if spec.time and spec.time.grain else ""
    inner = f"SELECT {', '.join(cols)}\nFROM {base}" + "".join(f"\n{j}" for j in joins)
    inner += ("\nWHERE " + " AND ".join(where)) if where else ""
    if groups:
        inner += "\nGROUP BY " + ", ".join(str(i + 1) for i in range(len(groups)))
    if spec.shares:
        names = [n for n, _ in groups] + list(selects)
        outer = ", ".join([f"q.{n}" for n in names] + [f"q.{s} * 1.0 / NULLIF(SUM(q.{s}) OVER ({partition}), 0) "
                                                       f"AS {s}_share" for s in spec.shares])
        sql = f"SELECT {outer}\nFROM ({inner}) AS q"
    else:
        sql = inner
    if spec.ranking:
        direction = "DESC" if spec.ranking.order == "desc" else "ASC"
        sql += f"\nORDER BY {spec.ranking.by} {direction} NULLS LAST\nLIMIT {spec.ranking.limit}"
    elif spec.time and spec.time.grain:
        sql += f"\nORDER BY {spec.time.grain}"
    elif groups and spec.measures:
        sql += f"\nORDER BY {spec.measures[0].name} DESC NULLS LAST"
    sqlglot.parse_one(sql, read="duckdb")                # never hand out SQL that does not parse
    return sql


# ----------------------------------------------------------------------------- loading
@lru_cache
def default_catalog(con=None) -> Catalog:
    from .catalog import label_columns, load_catalog
    from .db import connect
    from .profile import build_profile
    from .semantic import load_concepts, load_metrics, load_schema

    con = con or connect()
    schema = load_schema()
    labels = {k: v for k, v in label_columns().items()}
    return Catalog(schema, load_metrics(), load_concepts(), JoinGraph(schema, build_profile(con, schema)),
                   load_catalog(), labels, con)
