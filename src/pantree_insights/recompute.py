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
    for column, val in filters:
        where.append(f"{column} = '{str(val).replace(chr(39), chr(39) * 2)}'")
    time = time_column or _full_names(source.time_column, aliases)[0]
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
def check_answer(agent, canonical, keys: list[str], ratio: bool = False,
                 tolerance: float = 0.01) -> tuple[str, list[str]]:
    """Compare the agent's result with the recomputed metric, matching columns by their values (the agent's
    column names are unknown). Returns ("verified" | "mismatch" | "unverified", problems).
    - every canonical key column must match an agent column whose values are among the canonical keys; the
      agent may return only some rows (top-N); if keys cannot be matched the answer is "unverified"
    - verified if some numeric agent column equals the metric on every aligned row (x100 accepted for ratios)"""
    import pandas as pd

    if agent is None or agent.empty or canonical.empty:
        return "unverified", []
    canon = canonical.copy()
    canon_keys = {k: [_key(v) for v in canon[k]] for k in keys}
    mapping: dict[str, str] = {}
    for k in keys:
        wanted = set(canon_keys[k]) - {"∅"}
        found = next((c for c in agent.columns if c not in mapping.values()
                      and set(_key(v) for v in agent[c]) <= wanted and agent[c].notna().any()), None)
        if found is None:
            return "unverified", []
        mapping[k] = found
    lookup = {tuple(canon_keys[k][i] for k in keys): canon["value"].iloc[i] for i in range(len(canon))}
    rows = [(tuple(_key(r[mapping[k]]) for k in keys), r) for _, r in agent.iterrows()] if keys else \
        [((), agent.iloc[0])]
    if not keys and len(agent) != 1:
        return "unverified", []
    if any(key not in lookup for key, _ in rows):
        return "unverified", []
    numeric = [c for c in agent.columns if c not in mapping.values() and pd.api.types.is_numeric_dtype(agent[c])
               and agent[c].dtype != bool]
    scales = [1.0, 100.0] if ratio else [1.0]

    def close(got, want, scale):
        if pd.isna(got) and pd.isna(want):
            return True
        if pd.isna(got) or pd.isna(want):
            return False
        return abs(float(got) - float(want) * scale) <= max(tolerance * abs(float(want) * scale), 1e-6)

    for col in numeric:
        if any(all(close(r[col], lookup[key], s) for key, r in rows) for s in scales):
            return "verified", []
    sample = [f"{', '.join(key) or 'total'}: recomputed {lookup[key]:,.4g}" for key, _ in rows[:3]
              if not pd.isna(lookup[key])]
    return "mismatch", [f"No column of the result matches the metric recomputed from its definition for the same "
                        f"scope ({'; '.join(sample)}). Check the formula, filters and period against the definition."]
