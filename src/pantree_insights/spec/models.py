"""Spec: models."""
from __future__ import annotations

import re
from datetime import date
from functools import lru_cache
from typing import Literal

import sqlglot
from pydantic import BaseModel, Field, model_validator
from sqlglot import exp

from ..joins import JoinGraph
from ..semantic import ConceptEntry, Concepts, Metrics, Schema

GRAINS = ("day", "week", "month", "quarter", "year")


TICKETS = "tickets"                          # virtual table: the tickets source (one row per support ticket)


@lru_cache(maxsize=128)
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
    partition_by: list[str] = Field(default_factory=list, description="Optional dimensions: top N within each group, "
                                   "such as top products within each brand. Must be requested splits.")


class Operation(BaseModel):
    kind: Literal["period_compare", "rolling_sum", "running_sum"]
    previous_start: date | None = None
    previous_end: date | None = None
    days: int = Field(7, ge=1, le=366, description="Calendar days including the current day, for rolling_sum")


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
    operation: Operation | None = None


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
