"""Spec: compiler."""
from __future__ import annotations

import re
from datetime import timedelta

import sqlglot

from .models import TICKETS, Catalog, Filter, Measure, QuerySpec, SpecError, _aliases, _full_names
from .validation import _filtered_metric, _literal, _tables, validate


def _rank(sql: str, spec: QuerySpec, cat: Catalog) -> str:
    ranking = spec.ranking
    direction = ranking.order.upper()
    order = f"{ranking.by} {direction} NULLS LAST"
    if not ranking.partition_by:
        return sql + f"\nORDER BY {order}\nLIMIT {ranking.limit}"
    if any(field not in spec.dimensions for field in ranking.partition_by):
        raise SpecError("per-group ranking must name dimensions included in the result")
    tree = sqlglot.parse_one(sql, read="duckdb")
    names = [e.alias_or_name for e in tree.selects]
    keys = []
    for field in ranking.partition_by:
        expression, name, _ = cat.resolve(field, spec.table)
        key = cat.labels.get(expression) or name
        if key not in names and _tables(expression):
            key = f"{_tables(expression)[0]}_{key}"
        if key not in names:
            raise SpecError("per-group ranking dimension is not available in the result")
        keys.append(f'"{key}"')
    # Aggregate first; rank result groups afterwards. No measure can be inflated by this wrapper.
    partition = ", ".join(dict.fromkeys(keys))
    tie_break = ", ".join(f'"{n}" ASC NULLS LAST' for n in names if n != ranking.by)
    ordering = order + (", " + tie_break if tie_break else "")
    selected = ", ".join(f'"{n}"' for n in names)
    return (f"SELECT {selected} FROM ({sql}) AS ranked_groups "
            f"QUALIFY ROW_NUMBER() OVER (PARTITION BY {partition} ORDER BY {ordering}) <= {ranking.limit} "
            f"ORDER BY {partition}, {ordering}")


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
    if spec.operation:
        from ..operations import compile_operation
        return compile_operation(spec, cat)
    if len({cat.source_table(m.metric) for m in spec.measures if m.metric in cat.metrics.metrics}) > 1:
        return compile_multifact(spec, cat)
    if any(not re.fullmatch(r"[a-z][a-z0-9_]*", m.name) for m in spec.measures):
        raise SpecError("measure names must be lowercase SQL identifiers")
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
            # Filters on a one-to-many relationship mean membership, not replication of base rows.
            if not join and len(targets) == 1 and targets[0] != table and not f.op.endswith("_field"):
                target = targets[0]
                path = cat.graph.path(table, [target]) if table != TICKETS else []
                if path and any(s.fans_out for s in path) and not any(s.right in joined for s in path):
                    first, *rest = path
                    nested = first.right + "".join(f" JOIN {s.right} ON {s.on}" for s in rest)
                    join = f"{nested} ON {first.on}"
            if join and targets[0] not in joined:
                memberships.setdefault(join, []).append(condition(f, register=False))
            else:
                clauses.append(condition(f))
        for join, predicates in memberships.items():
            target, on = join.rsplit(" ON ", 1)
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
            if len(m.ratio) != 2 or any(r not in selects for r in m.ratio):
                raise SpecError(f"{m.name}: ratio must name two non-ratio measures")
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
    # Labels add their identity key. Explicitly asking for that key must not add it twice.
    groups = list(dict.fromkeys(groups))
    aliases = [name for name, _ in groups]
    groups = [(f"{_tables(expression)[0]}_{name}" if aliases.count(name) > 1 and _tables(expression) else name,
               expression) for name, expression in groups]
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
    output_names = [n for n, _ in groups] + [m.name for m in spec.measures] + [f"{s}_share" for s in spec.shares]
    if len(output_names) != len(set(output_names)):
        raise SpecError("result columns must have unique names; a measure collides with a split")
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
        sql = _rank(sql, spec, cat)
    elif spec.time and spec.time.grain:
        sql += f"\nORDER BY {spec.time.grain}"
    elif groups and spec.measures:
        sql += f"\nORDER BY {spec.measures[0].name} DESC NULLS LAST"
    sqlglot.parse_one(sql, read="duckdb")                # never hand out SQL that does not parse
    return sql


def compile_multifact(spec: QuerySpec, cat: Catalog) -> str:
    """Aggregate each fact at its own grain, then join the aggregates on conformed dimensions.

    No raw fact-to-fact joins, early ranking, zero filling of missing populations, or aggregation of ratios.
    Each stage uses the existing compiler and cardinality checks. Unsupported scopes fail explicitly.
    """
    if spec.columns or spec.limit or spec.shares or any(m.agg for m in spec.measures):
        raise SpecError("multi-fact plans support named metrics and their ratios, not row lists or raw aggregates")
    if spec.time and spec.time.field:
        raise SpecError("multi-fact periods use each metric's governed date; an explicit shared date is ambiguous")
    # Qualified fact attributes are not conformed merely because both stages can reach that fact.
    # Stable catalogue identity keys are safe references; every stage still checks join cardinality.
    for field in [*spec.dimensions, *(f.field for f in spec.filters)]:
        if "." in field:
            table, column = field.split(".", 1)
            facts = {cat.source_table(name) for name in cat.metrics.metrics}
            description = cat.schema.tables[table].columns.get(column, "") if table in cat.schema.tables else ""
            if table in facts or "primary key" not in description.lower():
                raise SpecError("multi-fact scopes need shared business concepts or a catalogue primary key")
    stages: dict[str, list[Measure]] = {}
    names = [m.name for m in spec.measures]
    if len(names) != len(set(names)) or any(not re.fullmatch(r"[a-z][a-z0-9_]*", n) for n in names):
        raise SpecError("multi-fact measure names must be unique lowercase SQL identifiers")
    for m in spec.measures:
        if m.metric:
            if m.metric not in cat.metrics.metrics:
                raise SpecError(f"unknown metric {m.metric}")
            stages.setdefault(cat.source_table(m.metric), []).append(m)
    if spec.table not in stages or len(stages) < 2 or len(stages) > 4:
        raise SpecError("multi-fact plans require two to four metric sources, including the base table")
    ctes, keys, expressions = [], None, {}
    for i, (table, measures) in enumerate(stages.items()):
        child = spec.model_copy(update={"table": table, "measures": measures, "ranking": None})
        problems = validate(child, cat)
        if problems:
            raise SpecError(f"{table} stage: {'; '.join(problems)}")
        tree = sqlglot.parse_one(compile_spec(child, cat), read="duckdb")
        tree.set("order", None)
        stage_keys = [e.alias_or_name for e in tree.selects if e.alias_or_name not in {m.name for m in measures}]
        if keys is not None and stage_keys != keys:
            raise SpecError("multi-fact dimensions do not have the same identity keys on every source")
        keys = stage_keys
        # Identical concept names alone are insufficient (e.g. support channel vs marketing channel).
        for d in [*spec.dimensions, *(f.field for f in spec.filters)]:
            reference = cat.resolve(d, spec.table)[0]
            if cat.resolve(d, table)[0] != reference:
                raise SpecError(f"{d} is not a conformed dimension across these sources")
        ctes.append(f"stage_{i} AS ({tree.sql(dialect='duckdb')})")
        expressions.update({m.name: f"stage_{i}.{m.name}" for m in measures})
    for m in spec.measures:
        if m.ratio:
            if m.where or len(m.ratio) != 2 or any(r not in {x.name for x in spec.measures if x.metric}
                                                  for r in m.ratio):
                raise SpecError(f"{m.name}: ratio must name two unfiltered stage measures")
            a, b = (expressions[r] for r in m.ratio)
            expressions[m.name] = f"({a}) * 1.0 / NULLIF({b}, 0)"
    count = len(stages)
    keys = keys or []
    if set(keys) & set(names):
        raise SpecError("a measure name collides with a split")
    coalesced = lambda k, n: "COALESCE(" + ", ".join(f"stage_{j}.{k}" for j in range(n)) + ")" if n > 1 \
        else f"stage_0.{k}"  # noqa: E731
    columns = [f"{coalesced(k, count)} AS {k}" for k in keys]
    columns += [f"{expressions[m.name]} AS {m.name}" for m in spec.measures]
    sql = "WITH " + ",\n".join(ctes) + "\nSELECT " + ", ".join(columns) + "\nFROM stage_0"
    for i in range(1, count):
        on = " AND ".join(f"{coalesced(k, i)} IS NOT DISTINCT FROM stage_{i}.{k}" for k in keys) or "TRUE"
        sql += f"\nFULL OUTER JOIN stage_{i} ON {on}"
    if spec.ranking:
        if spec.ranking.by not in names:
            raise SpecError("ranking must name a measure")
        sql = _rank(sql, spec, cat)
    elif keys:
        sql += "\nORDER BY " + ", ".join(keys)
    return sql
