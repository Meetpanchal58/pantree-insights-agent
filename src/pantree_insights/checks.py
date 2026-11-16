"""Result checks in code: problems found deterministically, before the LLM validator looks at the result.

- every filter the understanding step resolved (column = value) must be in the SQL, on that column
- no repeated rows (a sign of a join that duplicated rows, or a missing GROUP BY column)
- no column that is entirely empty (a CASE or join that never matched)
- rates and shares between 0% and 100%: a rate above 100% almost always means the numerator counts rows the
  denominator does not (rows repeated by a join). Percentages on a 0-100 scale, growth, change, lift, ROAS and
  other ratios may exceed 1; margins may be negative.

Filter enforcement is checked even for empty results; the validator decides whether emptiness is correct.
"""
from __future__ import annotations

import re
from collections import defaultdict

import pandas as pd
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, build_scope

from .guardrails import _qualify
from .semantic import load_schema

RATE = re.compile(r"(rate|share|pct|percent)", re.I)
UNBOUNDED = re.compile(r"(growth|change|lift|diff|ratio|roas|factor|per_|vs_|index|uplift|_x$)", re.I)
PERCENT_SCALE = re.compile(r"(pct|percent)", re.I)
MAY_BE_NEGATIVE = re.compile(r"(margin|growth|change|lift|diff)", re.I)


def _uses_filter(tree: exp.Expression, column: str, value: str | list[str]) -> bool:
    """Prove the requested values constrain every contributing scan, following simple CTE projections.

    SELECT/CASE/aggregate FILTER expressions are not row filters. OR must constrain both branches;
    an optional outer-join side cannot filter preserved rows. Unsupported lineage is not certified.
    """
    table, name = column.lower().split(".")
    wanted = frozenset([value] if isinstance(value, str) else value)
    root = build_scope(tree)
    if root is None:
        return False

    def origin(col, scope, path=()):
        # A CTE may rename the field; trace direct projections before checking the base name.
        if not isinstance(col, exp.Column):
            return set()
        source = scope.sources.get(col.table)
        if isinstance(source, exp.Table):
            return {(path, id(source))} if source.name.lower() == table and col.name.lower() == name else set()
        if isinstance(source, Scope):
            if source.set_operation_scopes:
                result = set()
                for i, branch in enumerate(source.set_operation_scopes):
                    index = next((i for i, e in enumerate(source.expression.selects)
                                  if e.alias_or_name == col.name), None)
                    if index is not None:
                        expr = branch.expression.selects[index]
                        expr = expr.this if isinstance(expr, exp.Alias) else expr
                        result |= origin(expr, branch, (*path, col.table, f"branch:{i}"))
                return result
            expr = next((e for e in source.expression.selects if e.alias_or_name == col.name), None)
            if expr is None:
                return set()
            expr = expr.this if isinstance(expr, exp.Alias) else expr
            return origin(expr, source, (*path, col.table))
        return set()

    def combine(left, right):
        merged = dict(left)
        for key, values in right.items():
            merged[key] = merged[key] & values if key in merged else values
        return merged

    def restrictions(predicate, scope, path):
        if isinstance(predicate, (exp.Paren, exp.Where)):
            return restrictions(predicate.this, scope, path)
        if isinstance(predicate, exp.And):
            return combine(restrictions(predicate.left, scope, path), restrictions(predicate.right, scope, path))
        if isinstance(predicate, exp.Or):
            left, right = restrictions(predicate.left, scope, path), restrictions(predicate.right, scope, path)
            return {k: left[k] | right[k] for k in left.keys() & right.keys()}
        if isinstance(predicate, exp.EQ):
            a, b = predicate.left, predicate.right
            if isinstance(a, exp.Literal):
                a, b = b, a
            if isinstance(b, exp.Literal) and b.is_string:
                return {key: frozenset([b.this]) for key in origin(a, scope, path)}
        if (isinstance(predicate, exp.In) and predicate.expressions and not predicate.args.get("query")
                and all(isinstance(e, exp.Literal) and e.is_string for e in predicate.expressions)):
            values = frozenset(e.this for e in predicate.expressions)
            return {key: values for key in origin(predicate.this, scope, path)}
        return {}

    def prove(scope, path=()):
        if scope.set_operation_scopes:
            required, known = set(), {}
            for i, child in enumerate(scope.set_operation_scopes):
                req, constraints = prove(child, (*path, f"branch:{i}"))
                required |= req
                known.update(constraints)
            return required, known
        required, inherited = set(), {}
        for alias, (_, source) in scope.selected_sources.items():
            if isinstance(source, exp.Table):
                req = {(path, id(source))} if source.name.lower() == table else set()
                constraints = {}
            else:
                req, constraints = prove(source, (*path, alias))
            required |= req
            inherited[alias] = constraints
        optional, earlier = set(), []
        from_ = scope.expression.args.get("from_")
        if from_ is not None:
            earlier = [from_.this.alias_or_name]
        for join in scope.expression.args.get("joins") or []:
            alias = join.this.alias_or_name
            if join.side == "LEFT":
                optional.add(alias)
            elif join.side == "RIGHT":
                optional.update(earlier)
            elif join.side == "FULL":
                optional.update([*earlier, alias])
            earlier.append(alias)
        known = {}
        for alias, constraints in inherited.items():
            if alias not in optional:
                known = combine(known, constraints)
        # Only WHERE and INNER JOIN predicates necessarily constrain the returned rows.
        known = combine(known, restrictions(scope.expression.args.get("where"), scope, path))
        for join in scope.expression.args.get("joins") or []:
            if not join.side and join.kind not in {"ANTI", "SEMI"}:
                known = combine(known, restrictions(join.args.get("on"), scope, path))
        return required, known

    required, known = prove(root)
    return bool(required) and all(known.get(key) == wanted for key in required)


def result_problems(frame: pd.DataFrame, sql: str, filters: list[tuple[str, str]]) -> list[str]:
    """Problems with a query result that code can see for sure; [] when there are none (or the result is empty)."""
    problems = []
    try:
        tree, _ = _qualify(sqlglot.parse_one(sql, read="duckdb"), load_schema())
    except sqlglot.errors.ParseError:
        tree = None
    grouped: dict[str, list[str]] = defaultdict(list)
    for column, value in filters:
        grouped[column].append(value)
    for column, values in grouped.items():
        if tree is None or not _uses_filter(tree, column, values):
            label = f"{column} = '{values[0]}'" if len(values) == 1 else f"{column} IN {values}"
            problems.append(f"The question was resolved to the filter {label}, but the query does not enforce "
                            "it on the contributing rows. Put the exact filter in WHERE (with the join it needs).")
    if frame is None or frame.empty:
        return problems
    if frame.duplicated().any():
        problems.append(f"{int(frame.duplicated().sum())} result rows repeat exactly: a join probably duplicated "
                        "rows or a GROUP BY column is missing. Aggregate the many side in its own CTE first.")
    for col in frame.columns:
        if frame[col].isna().all():
            problems.append(f"Column {col} is empty in every row: its CASE condition or join never matches.")
    for col in frame.columns:
        if not RATE.search(str(col)) or UNBOUNDED.search(str(col)) or not pd.api.types.is_numeric_dtype(frame[col]):
            continue
        values = frame[col].dropna()
        if values.empty or ((values % 1 == 0).all() and values.max() > 1):     # whole numbers: a count, not a rate
            continue
        upper = 100.0 if PERCENT_SCALE.search(str(col)) and values.max() > 1 else 1.0
        if values.max() > upper * 1.0001:
            problems.append(f"Column {col} goes above 100% (max {values.max():.3g}): the numerator probably counts "
                            "rows the denominator does not, e.g. lines repeated by a join. Check both sides.")
        if values.min() < 0 and not MAY_BE_NEGATIVE.search(str(col)):
            problems.append(f"Column {col} has negative values (min {values.min():.3g}), which a rate cannot have.")
    return problems
