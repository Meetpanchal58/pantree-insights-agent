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
    entity: str = ""                 # what one row describes ("order line", "seller"); tells shared column names apart
    grain: str
    description: str
    time_column: str | None = None
    columns: dict[str, str]          # column -> "<type> - <note>"


class Blocked(BaseModel):
    tables: dict[str, str] = {}      # table -> reason
    columns: dict[str, str] = {}     # "table.column" -> reason

    def column_names(self) -> set[str]:
        return {key.split(".", 1)[1] for key in self.columns}


class JoinRule(BaseModel):
    extra: str                       # extra ON condition, written with full table names
    why: str


class Schema(BaseModel):
    tables: dict[str, Table]
    joins: list[str]
    join_rules: dict[str, JoinRule] = {}
    identifier_columns: list[str] = []
    blocked: Blocked

    def column_types(self) -> dict[str, dict[str, str]]:
        """{table: {column: duckdb type}} for sqlglot's qualify (resolves unqualified columns)."""
        kinds = {"text": "VARCHAR", "number": "DOUBLE", "integer": "BIGINT", "timestamp": "TIMESTAMP",
                 "boolean": "BOOLEAN"}
        return {t: {c: kinds.get(d.split(" - ")[0].strip(), "VARCHAR") for c, d in info.columns.items()}
                for t, info in self.tables.items()}

    @model_validator(mode="after")
    def _check(self) -> Schema:
        for table in self.blocked.tables:
            if table in self.tables:
                raise ValueError(f"blocked table {table} is also listed as allowed")
        for key in self.blocked.columns:
            table, column = key.split(".", 1)
            if column in self.tables.get(table, Table(group="", grain="", description="", columns={})).columns:
                raise ValueError(f"blocked column {key} is still listed in schema.yaml")
        for join in self.join_rules:
            if join not in self.joins:
                raise ValueError(f"join rule for {join}, which is not an approved join")
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
class VerifySpec(BaseModel):
    """How to recompute one result column from metrics.yaml (recompute.py) to check a reference query's numbers."""
    metric: str
    column: str                      # result column holding the metric
    by: dict[str, str] = {}          # result key column -> SQL expression with full table names
    where: list[str] = []            # extra conditions with full table names
    time: str | None = None          # time column if not the metric source's default
    partial: bool = False            # the query returns only some rows (top-N, HAVING): compare those


class Example(BaseModel):
    id: str
    question: str
    sql: str
    difficulty: Literal["easy", "medium", "hard"]
    tags: list[str] = []
    notes: str | None = None
    area: str | None = None          # query library only: sales, funnel, customers, marketing, ...
    variants: list[str] = []         # other ways people ask the same thing (searched by retrieval)
    metrics: list[str] = []          # metrics.yaml names the query computes (checked by tests)
    verify: list[VerifySpec] = []    # recomputation checks of its numbers (tests/test_library.py)


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
    term_tables: dict[str, list[str]] = {}       # business phrase -> tables it needs (added by retrieval)

    def terms_in(self, text: str) -> dict[str, str]:
        """Glossary terms mentioned in a question (whole words, any case)."""
        return {term: meaning for term, meaning in self.terms.items() if _mentions(term, text)}

    def tables_in(self, text: str) -> list[str]:
        """Tables named by the glossary phrases a question uses, in glossary order."""
        return list(dict.fromkeys(t for phrase, tables in self.term_tables.items() if _mentions(phrase, text)
                                  for t in tables))


def _mentions(phrase: str, text: str) -> bool:
    """Whole-word match, any case; a trailing s is optional ("city tiers" mentions "city tier")."""
    return re.search(rf"(?<![\w-]){re.escape(phrase)}s?(?![\w-])", text, re.I) is not None


# ----------------------------------------------------------------------------- concepts.yaml
class Subject(BaseModel):
    table: str
    about: str
    time: str                                    # default time column ("table.column")
    where: str | None = None                     # filter that always applies (e.g. sale lines only)
    time_meanings: dict[str, str] = {}           # "delivered" -> sales.delivered_at


class ConceptEntry(BaseModel):
    column: str                                  # "table.column" (the stored column; decides the join path)
    note: str | None = None
    expression: str | None = None                # derived cut, e.g. COD vs Prepaid from payment_method

    def sql(self) -> str:
        return self.expression or self.column

    def labels(self) -> list[str]:
        """The values a derived cut can take ('COD', 'Prepaid'): the string literals its expression returns."""
        if not self.expression:
            return []
        return list(dict.fromkeys(re.findall(r"(?:THEN|ELSE)\s+'([^']*)'", self.expression, re.I)))


class ConceptInfo(BaseModel):
    label: str
    group: str


class Concepts(BaseModel):
    subjects: dict[str, Subject]
    concepts: dict[str, dict[str, ConceptEntry]]
    concept_info: dict[str, ConceptInfo] = {}
    id_entities: dict[str, str] = {}             # "order" -> "sales.order_id"

    @model_validator(mode="before")
    @classmethod
    def _short_form(cls, data: dict) -> dict:
        """`city: {orders: sales.ship_city}` is short for {orders: {column: sales.ship_city}}."""
        concepts = {name: {subject: {"column": v} if isinstance(v, str) else v for subject, v in entries.items()}
                    for name, entries in (data.get("concepts") or {}).items()}
        return {**data, "concepts": concepts}

    @model_validator(mode="after")
    def _check(self) -> Concepts:
        for name, entries in self.concepts.items():
            unknown = set(entries) - set(self.subjects)
            if unknown:
                raise ValueError(f"concept {name}: unknown subjects {sorted(unknown)}")
        missing = set(self.concepts) ^ set(self.concept_info)
        if self.concept_info and missing:
            raise ValueError(f"concepts and concept_info differ: {sorted(missing)}")
        return self

    def label(self, concept: str) -> str:
        info = self.concept_info.get(concept)
        return info.label if info else concept.replace("_", " ")

    def group(self, concept: str) -> str:
        info = self.concept_info.get(concept)
        return info.group if info else concept

    def column(self, concept: str, subject: str) -> ConceptEntry | None:
        """The column a concept means for a subject, or None if that cut does not exist for it."""
        return self.concepts.get(concept, {}).get(subject)

    def concepts_of_column(self, qualified: str) -> set[str]:
        """'customers.city' -> {'city'}: which concepts a column can stand for (any subject)."""
        return {name for name, entries in self.concepts.items() if any(e.column == qualified for e in entries.values())}


# ----------------------------------------------------------------------------- loading
def _read(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@lru_cache
def load_concepts(folder: Path | None = None) -> Concepts:
    folder = folder or get_settings().semantic_dir
    return Concepts.model_validate(with_catalog_concepts(_read(folder / "concepts.yaml"), folder))


def with_catalog_concepts(raw: dict, folder: Path) -> dict:
    """Every category or yes/no column of a subject's own table is a cut of that subject, even when concepts.yaml
    does not list it (order status, cancel reason, list surface, colour ...): without it, a split by that column
    would be silently dropped from the understanding, the read-back and the number check. Hand-written concepts
    win; these only fill the gaps, named after the column."""
    from .catalog import load_catalog  # catalog.py imports this module

    catalog = load_catalog(folder)
    if not catalog:
        return raw
    concepts = {name: dict(entries) for name, entries in (raw.get("concepts") or {}).items()}
    info = dict(raw.get("concept_info") or {})
    for subject, spec in (raw.get("subjects") or {}).items():
        table = spec["table"]
        mapped = {(e if isinstance(e, str) else e["column"]) for entries in concepts.values()
                  for s, e in entries.items() if s == subject}
        for column, entry in (catalog.get(table) or {}).get("columns", {}).items():
            qualified = f"{table}.{column}"
            if (entry["role"] not in ("category", "flag") or qualified in mapped
                    or "not for analysis" in entry["meaning"]):
                continue
            concepts.setdefault(column, {})[subject] = {"column": qualified, "note": entry["meaning"]}
            info.setdefault(column, {"label": column.replace("_", " "), "group": column})
    return {**raw, "concepts": concepts, "concept_info": info}


def load_library(folder: Path | None = None) -> list[Example]:
    """The query library: semantic/library/<area>.yaml, each {area, queries: [...]}. Reference queries for
    retrieval only; evaluation scores the verified examples in examples.yaml (load_examples)."""
    out: list[Example] = []
    for path in sorted(((folder or get_settings().semantic_dir) / "library").glob("*.yaml")):
        raw = _read(path)
        out += [Example(**{"area": raw["area"], **q}) for q in raw["queries"]]
    ids = [e.id for e in out]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate query ids in semantic/library")
    return out


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
