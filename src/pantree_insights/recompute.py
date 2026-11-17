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


# ----------------------------------------------------------------------------- comparing numbers
NULL_LABELS = {"unknown", "n/a", "na", "none", "not set", "not given", "blank", "(blank)", "missing", "other/unknown",
               "unspecified", "null", "-"}


def _key(value) -> str:
    if value is None or (isinstance(value, float) and value != value):
        return "∅"
    text = str(value).strip()                     # case kept: 'ADIDAS' and 'Adidas' are different stored values
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    if text.lower() in ("true", "false"):
        return text.lower()
    try:
        number = float(text)
        return str(int(number)) if number.is_integer() else f"{number:.6g}"
    except ValueError:
        return text


def compare(result, canonical, keys: list[str], column: str, partial: bool = False,
            tolerance: float = 0.01) -> list[str]:
    """Problems where `result[column]` disagrees with the recomputed `canonical['value']`, row by row on `keys`.
    Canonical rows with an empty key (rows a LEFT JOIN could not place) are ignored unless the result has them;
    `partial`: the result may hold only some of the rows (top-N, HAVING)."""
    import pandas as pd

    def table(frame, col):
        return {tuple(_key(v) for v in row[:-1]): row[-1] for row in frame[[*keys, col]].itertuples(index=False)}

    mine, ref = table(result, column), table(canonical, "value")
    if not any("∅" in k for k in mine):
        ref = {k: v for k, v in ref.items() if "∅" not in k}
    problems = []
    for k, got in mine.items():
        if k not in ref:
            problems.append(f"row {k}: not in the recomputed result")
            continue
        want = ref[k]
        if pd.isna(got) and pd.isna(want):
            continue
        if pd.isna(got) or pd.isna(want) or abs(float(got) - float(want)) > max(tolerance * abs(float(want)), 1e-6):
            problems.append(f"row {k}: {column} = {got} but recomputed {want}")
    if not partial:
        missing = set(ref) - set(mine)
        if missing:
            problems.append(f"{len(missing)} recomputed rows missing, e.g. {sorted(missing)[:3]}")
    return problems


# ----------------------------------------------------------------------------- runtime: the agent's answer
def check_answer(agent, canonical, keys: list[str], ratio: bool = False, tolerance: float = 0.01,
                 *, column: str | None = None, limit: int | None = None,
                 ranking: str | None = None) -> tuple[str, list[str]]:
    """Verify a named metric and named dimensions, never infer their identity from coincidental values.

    A subset is permitted only for an explicit highest/lowest ranking with a confirmed row limit.
    Unknown aliases/unsupported shapes stay unverified rather than receiving a green badge.
    """
    import pandas as pd

    if agent is None or column is None or column not in agent or any(k not in agent for k in keys):
        return "unverified", []
    if canonical.empty:
        return ("verified", []) if agent.empty else ("mismatch", ["Expected an empty result for this scope."])
    if agent.empty:
        return "mismatch", ["All recomputed rows are missing."]
    if not pd.api.types.is_numeric_dtype(agent[column]) or agent[column].dtype == bool:
        return "unverified", []
    canon = canonical.copy()
    mine_keys = [tuple(_key(v) for v in row) for row in agent[keys].itertuples(index=False, name=None)] \
        if keys else [()] * len(agent)
    ref_keys = [tuple(_key(v) for v in row) for row in canon[keys].itertuples(index=False, name=None)] \
        if keys else [()] * len(canon)
    if len(set(ref_keys)) != len(ref_keys):
        return "unverified", []
    if len(set(mine_keys)) != len(mine_keys):
        # Several result rows per confirmed group: the result is split by something the confirmed scope does not
        # know (a date grain, a bucket, a second cut). The check cannot judge that grain, so it never rejects it;
        # exact repeated rows are caught separately by the result checks.
        return "unverified", []
    if any("∅" in k for k in ref_keys):                 # "Unknown" / "N/A" written for an empty group
        mine_keys = [tuple("∅" if part.casefold() in NULL_LABELS and part not in {p for r in ref_keys for p in r}
                           else part for part in key) for key in mine_keys]
    lookup = dict(zip(ref_keys, canon["value"], strict=True))
    if set(mine_keys) - set(ref_keys):
        return "mismatch", ["Result contains groups outside the confirmed scope."]
    partial = limit is not None and ranking in {"highest", "lowest"} and set(mine_keys) != set(ref_keys)
    if partial:
        # A ranking may return its top N or more (every group, ranked): more rows are checked number by number
        # below. Fewer rows than N, or a group outside the true top N in place of one inside it, is wrong.
        if len(agent) < min(limit, len(canon)):
            return "mismatch", ["The result has fewer ranked groups than confirmed."]
        ranked = canon["value"].dropna().sort_values(ascending=ranking == "lowest")
        expected_non_null = min(limit, len(ranked))
        if expected_non_null:
            top = {key for key, value in zip(ref_keys, canon["value"], strict=True)
                   if not pd.isna(value) and (value >= ranked.iloc[expected_non_null - 1] if ranking == "highest"
                                              else value <= ranked.iloc[expected_non_null - 1])}
            if not top <= set(mine_keys):
                return "mismatch", ["The returned groups are not the confirmed highest/lowest groups."]
    elif set(ref_keys) - set(mine_keys):
        return "mismatch", ["Recomputed groups are missing; no partial ranking was confirmed."]

    def close(got, want, scale):
        if pd.isna(got) and pd.isna(want):
            return True
        if pd.isna(got) or pd.isna(want):
            return False
        return abs(float(got) - float(want) * scale) <= max(tolerance * abs(float(want) * scale), 1e-6)

    if any(all(close(got, lookup[key], scale) for key, got in zip(mine_keys, agent[column], strict=True))
           for scale in ([1.0, 100.0] if ratio else [1.0])):
        return "verified", []
    sample = "; ".join(f"{', '.join(key) or 'total'}: {lookup[key]}" for key in mine_keys[:3])
    return "mismatch", [f"{column} disagrees with the metric recomputed from its definition for the confirmed "
                        f"filters, period and grain ({sample})."]


# ----------------------------------------------------------------------------- is the scope the query's population?
def extra_filter_columns(sql: str, allowed: set[str]) -> set[str]:
    """Columns the query filters rows on (WHERE, HAVING) that the confirmed scope does not know. `allowed` holds
    column names (no table). Conditions inside an aggregate's FILTER are measure conditions, not row filters."""
    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
    except sqlglot.errors.ParseError:
        return {"(unparsed)"}
    found = set()
    for clause in tree.find_all(exp.Where, exp.Having):
        if isinstance(clause, exp.Where) and isinstance(clause.parent, exp.Filter):
            continue
        for col in clause.find_all(exp.Column):
            name = col.name.lower()
            if name not in allowed:
                found.add(name)
    return found
