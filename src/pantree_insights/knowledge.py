"""Versioned retrieval records generated from the reviewed business definitions, never eval answers or user memory."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import sqlglot
import yaml
from sqlglot import exp

from .config import get_settings
from .semantic import Schema
from .templates import TEMPLATES, build

FORMAT_VERSION = 2


@dataclass(frozen=True)
class Record:
    id: str
    kind: str
    title: str
    text: str
    source: str
    dependencies: tuple[str, ...] = ()

    def render(self) -> str:
        return f"[{self.id}] {self.title}\nSource: {self.source}\n{self.text}"


def join_id(join: str) -> str:
    return "join:" + re.sub(r"\s+", "", join)


def build_records(folder: Path | None = None) -> list[Record]:
    folder = folder or get_settings().semantic_dir
    read = lambda name: yaml.safe_load((folder / name).read_text())  # noqa: E731
    raw_schema = read("schema.yaml")
    schema = Schema.model_validate(raw_schema)
    metrics, concepts, glossary = read("metrics.yaml"), read("concepts.yaml"), read("glossary.yaml")
    catalog = read("catalog.yaml")["tables"]
    records = []

    def add(id, kind, title, text, source, deps=()):
        records.append(Record(id, kind, title, text, source, tuple(sorted(set(deps)))))

    def tables_in(sql):
        return sorted({t.name for t in sqlglot.parse_one(sql, read="duckdb").find_all(exp.Table)
                       if t.name in schema.tables})

    def dependencies(tables):
        # Include all approved direct relationships among these tables. Runtime cardinality
        # checks remain authoritative; this graph documents possible paths, not safe query plans.
        return [*(f"table:{t}" for t in tables),
                *(join_id(j) for j in schema.joins
                  if {side.strip().split(".")[0] for side in j.split("=")} <= set(tables))]

    for table, info in schema.tables.items():
        subjects = [s for s in concepts["subjects"].values() if s["table"] == table]
        rules = "; ".join(s.get("where", "") or "" for s in subjects)
        columns = "\n".join(f"{table}.{c}: {d}" for c, d in info.columns.items())
        add(f"table:{table}", "table", f"{table}: {info.entity}",
            f"Grain: {info.grain}. {info.description}\nMain date: {info.time_column}. "
            f"Population rule: {rules or 'none'}.\nSubject and date meanings: {json.dumps(subjects)}\n"
            f"Columns:\n{columns}", f"semantic/schema.yaml#tables/{table}")
        for col, meaning in info.columns.items():
            metadata = catalog.get(table, {}).get("columns", {}).get(col, {})
            values = metadata.get("values") or []
            text = f"{table}.{col}: {meaning}\nTable grain: {info.grain}."
            if values:
                text += ("\nCatalogued values (snapshot metadata; confirm current values before use): "
                         + json.dumps(values))
            add(f"column:{table}.{col}", "column", f"{table}.{col}", text,
                f"semantic/schema.yaml#tables/{table}/columns/{col}", [f"table:{table}"])

    for join in schema.joins:
        tables = [side.strip().split(".")[0] for side in join.split("=")]
        rule = schema.join_rules.get(join)
        text = f"Approved relationship: {join}. Measure cardinality before joining aggregates."
        if rule:
            text += f" Required condition: {rule.extra}. Reason: {rule.why}"
        add(join_id(join), "join", join, text, "semantic/schema.yaml#joins", [f"table:{t}" for t in tables])

    for name, metric in metrics["metrics"].items():
        source = metrics["sources"][metric["source"]]
        text = json.dumps({"metric": name, **metric, "source_definition": source}, ensure_ascii=False)
        add(f"metric:{name}", "metric", f"{name}: {metric['label']}", text,
            f"semantic/metrics.yaml#metrics/{name}", dependencies(tables_in(f"SELECT 1 FROM {source['from']}")))

    for name, metric in metrics.get("custom_metrics", {}).items():
        add(f"metric:{name}", "metric", f"{name}: {metric.get('label', name)}",
            json.dumps(metric, ensure_ascii=False), f"semantic/metrics.yaml#custom_metrics/{name}",
            dependencies(tables_in(metric["sql"])))

    for name, per_subject in concepts["concepts"].items():
        for subject, entry in per_subject.items():
            table = concepts["subjects"][subject]["table"]
            text = entry if isinstance(entry, str) else json.dumps(entry, ensure_ascii=False)
            tables = sorted({table, *(t for t in re.findall(r"\b(\w+)\.\w+", text) if t in schema.tables)})
            add(f"concept:{subject}.{name}", "concept", f"{name} for {subject}", text,
                f"semantic/concepts.yaml#concepts/{name}/{subject}", dependencies(tables))

    for name, definition in glossary.get("terms", {}).items():
        normalized = definition.lower().replace("_", " ")
        metric_deps = [f"metric:{m}" for m in {**metrics["metrics"], **metrics.get("custom_metrics", {})}
                       if re.search(rf"\b{re.escape(m.replace('_', ' '))}\b", normalized)]
        tables = {t for t in re.findall(r"\b(\w+)\.\w+", definition) if t in schema.tables}
        add(f"term:{name}", "term", name, definition, f"semantic/glossary.yaml#terms/{name}",
            [*metric_deps, *(f"table:{t}" for t in tables)])
    for name, meanings in glossary.get("ambiguous", {}).items():
        add(f"ambiguity:{name}", "ambiguity", name, "Clarify the meaning: " + "; ".join(meanings),
            f"semantic/glossary.yaml#ambiguous/{name}")
    for name, template in TEMPLATES.items():
        sql, explanation = build(name, {})
        add(f"analysis:{name}", "analysis", name, f"{template.description}\n{explanation}\nReviewed SQL:\n{sql}",
            f"src/pantree_insights/templates.py#{name}", dependencies(tables_in(sql)))
    ids = {r.id for r in records}
    if len(ids) != len(records):
        raise ValueError("Duplicate knowledge record IDs")
    for record in records:
        if missing := set(record.dependencies) - ids:
            raise ValueError(f"{record.id}: missing dependencies {missing}")
    return sorted(records, key=lambda r: r.id)


def fingerprint(records: list[Record]) -> str:
    body = json.dumps({"format": FORMAT_VERSION, "records": [asdict(r) for r in records]}, sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()
