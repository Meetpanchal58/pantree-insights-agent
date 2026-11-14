"""The business layer: tables, metric definitions and verified examples from semantic/*.yaml."""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from .config import get_settings


# ----------------------------------------------------------------------------- schema.yaml
class Table(BaseModel):
    group: str
    grain: str
    description: str
    time_column: str | None = None
    columns: dict[str, str]          # column -> "<type> - <note>"


class Blocked(BaseModel):
    tables: dict[str, str] = {}      # table -> reason
    columns: dict[str, str] = {}     # "table.column" -> reason

    def column_names(self) -> set[str]:
        return {key.split(".", 1)[1] for key in self.columns}


class Schema(BaseModel):
    tables: dict[str, Table]
    joins: list[str]
    blocked: Blocked

    @model_validator(mode="after")
    def _check(self) -> Schema:
        for table in self.blocked.tables:
            if table in self.tables:
                raise ValueError(f"blocked table {table} is also listed as allowed")
        for key in self.blocked.columns:
            table, column = key.split(".", 1)
            if column in self.tables.get(table, Table(group="", grain="", description="", columns={})).columns:
                raise ValueError(f"blocked column {key} is still listed in schema.yaml")
        for join in self.joins:
            for side in join.split("="):
                table, column = side.strip().split(".")
                if column not in self.tables[table].columns:
                    raise ValueError(f"join {join}: {table}.{column} does not exist")
        return self

    def describe(self, names: list[str] | None = None) -> str:
        """Compact text description of some (or all) tables, for prompts."""
        out = []
        for name in names or list(self.tables):
            t = self.tables[name]
            cols = "\n".join(f"  - {c}: {d}" for c, d in t.columns.items())
            out.append(f"TABLE {name} ({t.grain}). {t.description}\n{cols}")
        return "\n\n".join(out)


# ----------------------------------------------------------------------------- metrics.yaml
class Source(BaseModel):
    """A base table (with optional joins) that metrics are computed from."""
    description: str
    from_: str = Field(alias="from")
    time_column: str
    where: str | None = None                    # filter applied to every metric on this source
    dimensions: dict[str, str]                  # breakdown name -> SQL expression


class Metric(BaseModel):
    label: str
    description: str
    source: str
    numerator: str                              # aggregate SQL expression
    denominator: str | None = None              # if set, value = numerator / denominator
    unit: Literal["inr", "count", "ratio", "days", "minutes", "rating"]
    higher_is_better: bool | None = None        # None = neither (e.g. order counts during events)
    notes: str | None = None

    def expression(self) -> str:
        if self.denominator:
            return f"({self.numerator}) / NULLIF({self.denominator}, 0)"
        return self.numerator


class CustomMetric(BaseModel):
    """A metric that needs more than one aggregate over one source: documented as full SQL."""
    label: str
    description: str
    unit: str
    sql: str


class Metrics(BaseModel):
    sources: dict[str, Source]
    metrics: dict[str, Metric]
    custom_metrics: dict[str, CustomMetric] = {}

    @model_validator(mode="after")
    def _check(self) -> Metrics:
        for name, metric in self.metrics.items():
            if metric.source not in self.sources:
                raise ValueError(f"metric {name}: unknown source {metric.source}")
        return self

    def sql(self, name: str, by: str | None = None, start: str | None = None, end: str | None = None) -> str:
        """Reference SQL for a metric, optionally broken down by one dimension and limited to [start, end)."""
        metric = self.metrics[name]
        source = self.sources[metric.source]
        where = [w for w in [source.where] if w]
        if start:
            where.append(f"{source.time_column} >= TIMESTAMP '{start}'")
        if end:
            where.append(f"{source.time_column} < TIMESTAMP '{end}'")
        select = f"{metric.expression()} AS {name}"
        clause = f"\nWHERE {' AND '.join(where)}" if where else ""
        if by:
            dim = source.dimensions[by]
            return (f"SELECT {dim} AS {by}, {select}\nFROM {source.from_}{clause}\n"
                    f"GROUP BY 1\nORDER BY 2 DESC")
        return f"SELECT {select}\nFROM {source.from_}{clause}"

    def describe(self, names: list[str] | None = None) -> str:
        out = []
        for name in names or list(self.metrics):
            if name in self.metrics:
                m = self.metrics[name]
                dims = ", ".join(self.sources[m.source].dimensions)
                out.append(f"METRIC {name} ({m.label}, {m.unit}): {m.description}\n"
                           f"  SQL: {self.sql(name)}\n  breakdowns: {dims}"
                           + (f"\n  note: {m.notes}" if m.notes else ""))
            elif name in self.custom_metrics:
                c = self.custom_metrics[name]
                out.append(f"METRIC {name} ({c.label}, {c.unit}): {c.description}\n  SQL: {c.sql.strip()}")
        return "\n\n".join(out)


# ----------------------------------------------------------------------------- examples.yaml
class Example(BaseModel):
    id: str
    question: str
    sql: str
    difficulty: Literal["easy", "medium", "hard"]
    tags: list[str] = []
    notes: str | None = None


class Examples(BaseModel):
    examples: list[Example]

    @model_validator(mode="after")
    def _unique(self) -> Examples:
        ids = [e.id for e in self.examples]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate example ids")
        return self


# ----------------------------------------------------------------------------- glossary.yaml
class Glossary(BaseModel):
    value_aliases: dict[str, list[str]] = {}     # stored value -> other names people use
    terms: dict[str, str] = {}                   # business word -> meaning

    def terms_in(self, text: str) -> dict[str, str]:
        """Glossary terms mentioned in a question (whole words, any case)."""
        return {term: meaning for term, meaning in self.terms.items()
                if re.search(rf"(?<![\w-]){re.escape(term)}(?![\w-])", text, re.I)}


# ----------------------------------------------------------------------------- loading
def _read(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@lru_cache
def load_schema(folder: Path | None = None) -> Schema:
    return Schema.model_validate(_read((folder or get_settings().semantic_dir) / "schema.yaml"))


@lru_cache
def load_metrics(folder: Path | None = None) -> Metrics:
    return Metrics.model_validate(_read((folder or get_settings().semantic_dir) / "metrics.yaml"))


@lru_cache
def load_examples(folder: Path | None = None) -> Examples:
    return Examples.model_validate(_read((folder or get_settings().semantic_dir) / "examples.yaml"))


@lru_cache
def load_glossary(folder: Path | None = None) -> Glossary:
    return Glossary.model_validate(_read((folder or get_settings().semantic_dir) / "glossary.yaml"))


def tables_in(sql: str) -> set[str]:
    """Rough list of table names a SQL string mentions (for picking relevant examples)."""
    return {name.lower() for name in re.findall(r"(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)", sql, re.I)}
