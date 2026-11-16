"""Result checks in code: problems found deterministically, before the LLM validator looks at the result.

- every filter the understanding step resolved (column = value) must be in the SQL, on that column
- no repeated rows (a sign of a join that duplicated rows, or a missing GROUP BY column)
- no column that is entirely empty (a CASE or join that never matched)
- rates and shares between 0% and 100%: a rate above 100% almost always means the numerator counts rows the
  denominator does not (rows repeated by a join). Percentages on a 0-100 scale, growth, change, lift, ROAS and
  other ratios may exceed 1; margins may be negative.

Empty results are not checked here: the validator decides whether 0 rows is correct.
"""
from __future__ import annotations

import re

import pandas as pd
import sqlglot
from sqlglot import exp

from .guardrails import _qualify
from .semantic import load_schema

RATE = re.compile(r"(rate|share|pct|percent)", re.I)
UNBOUNDED = re.compile(r"(growth|change|lift|diff|ratio|roas|factor|per_|vs_|index|uplift|_x$)", re.I)
PERCENT_SCALE = re.compile(r"(pct|percent)", re.I)
MAY_BE_NEGATIVE = re.compile(r"(margin|growth|change|lift|diff)", re.I)


def _uses_filter(tree: exp.Expression, column: str, value: str) -> bool:
    """Is table.column compared with `value` (=, IN) anywhere in the query? Columns are resolved to their table
    (sqlglot qualify against the schema), so customers.city does not count for sellers.city."""
    table, name = column.lower().split(".")
    wanted = value.lower()
    alias = {t.alias_or_name.lower(): t.name.lower() for t in tree.find_all(exp.Table)}
    tables = load_schema().tables

    def same_table(c: exp.Column) -> bool:
        resolved = alias.get((c.table or "").lower())
        return resolved is None or resolved == table or resolved not in tables     # CTE/subquery: cannot trace

    for node in [*tree.find_all(exp.EQ), *tree.find_all(exp.In)]:
        cols = [c for c in node.find_all(exp.Column) if c.name.lower() == name and same_table(c)]
        lits = [lit for lit in node.find_all(exp.Literal) if lit.is_string and lit.this.lower() == wanted]
        if cols and lits:
            return True
    return False


def result_problems(frame: pd.DataFrame, sql: str, filters: list[tuple[str, str]]) -> list[str]:
    """Problems with a query result that code can see for sure; [] when there are none (or the result is empty)."""
    if frame is None or frame.empty:
        return []
    problems = []
    try:
        tree, _ = _qualify(sqlglot.parse_one(sql, read="duckdb"), load_schema())
    except sqlglot.errors.ParseError:
        tree = None
    if tree is not None:
        for column, value in filters:
            if not _uses_filter(tree, column, value):
                problems.append(f"The question was resolved to the filter {column} = '{value}', but the query does "
                                f"not filter {column.split('.')[1]} on '{value}'. Add it (with the join it needs).")
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
