"""Read-only, bounded dataset browsing from governed metadata, with snapshot-scoped caches."""

from __future__ import annotations

import contextlib
import json
import re
import threading
from functools import lru_cache

import pyarrow.parquet as pq

from .config import get_settings
from .db import connect, manifest, snapshot_signature
from .profile import _from_json, published_path
from .semantic import load_metrics, load_schema
from .store import to_json

SAMPLE_ROWS = 30
DICTIONARY_ROWS = 512
_LOCK = threading.RLock()


def masked(table: str, column: str) -> bool:
    return bool(
        re.search(r"(?:^|_)(password|secret|token|api_key|phone|email|address_line|ip_address)$", column)
        or column == "agent_name"
        or table == "auth_events"
        and column.endswith("_id")
    )


def _identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _files(table: str):
    folder = get_settings().shop_dir
    single = folder / f"{table}.parquet"
    return [single] if single.is_file() else sorted((folder / table).glob("month=*/part.parquet"))


def overview() -> dict:
    with _LOCK:
        return _overview(snapshot_signature())


@lru_cache(maxsize=2)
def _overview(version: str) -> dict:
    schema, metrics, info = load_schema(), load_metrics(), manifest()
    path = published_path(info)
    measured = {}
    if path.exists():
        with contextlib.suppress(ValueError, KeyError, TypeError):
            measured = _from_json(path.read_text()).joins
    relationships = []
    foreign = set()
    for join in schema.joins:
        left, right = [side.strip() for side in join.split("=")]
        for source, target in ((left, right), (right, left)):
            target_table, target_column = target.split(".")
            if "primary key" in schema.tables[target_table].columns[target_column].lower():
                foreign.add(source)
        profile = measured.get(join)
        rule = schema.join_rules.get(join)
        relationships.append(
            {
                "left": left,
                "right": right,
                "cardinality": profile.cardinality if profile else "unverified",
                "precaution": rule.why
                if rule
                else "Aggregate each fact at its native grain before joining; avoid fan-out.",
                "condition": join + (" AND " + rule.extra if rule else ""),
                "measured": profile is not None,
            }
        )
    tables = []
    for name, table in schema.tables.items():
        if name in schema.blocked.tables or not _files(name):
            continue
        columns = [
            {
                "name": col,
                "description": desc.partition(" - ")[2] or desc,
                "key": "primary"
                if "primary key" in desc.lower()
                else "foreign"
                if f"{name}.{col}" in foreign
                else "ordinary",
                "masked": masked(name, col),
            }
            for col, desc in table.columns.items()
            if f"{name}.{col}" not in schema.blocked.columns
        ]
        uses = [
            m.label
            for m in metrics.metrics.values()
            if re.search(rf"\b{re.escape(name)}\b", metrics.sources[m.source].from_)
        ][:6]
        tables.append(
            {
                "name": name,
                "description": table.description,
                "grain": table.grain,
                "group": table.group,
                "date_column": table.time_column,
                "columns": columns,
                "column_count": len(columns),
                "primary_key": [c["name"] for c in columns if c["key"] == "primary"],
                "uses": uses,
            }
        )
    visible = {t["name"] for t in tables}
    relationships = [r for r in relationships if all(r[k].split(".")[0] in visible for k in ("left", "right"))]
    return {
        "version": version,
        "snapshot": info,
        "tables": tables,
        "relationships": relationships,
        "notice": "Governed simulator data. Sensitive fields are masked; dictionary values are bounded samples "
        "unless explicitly marked complete. Browsing uses no model or analytical allowance.",
    }


def preview(table: str, version: str | None = None) -> dict:
    with _LOCK:
        current = snapshot_signature()
        if version is not None and version != current:
            raise ValueError("The dataset changed. Reopen Dataset Preview to use the current snapshot.")
        if table not in {t["name"] for t in _overview(current)["tables"]}:
            raise KeyError("Table not available")
        return _preview(current, table)


@lru_cache(maxsize=48)
def _preview(version: str, table: str) -> dict:
    schema = load_schema()
    columns = [c for c in schema.tables[table].columns if f"{table}.{c}" not in schema.blocked.columns]
    # Footer metadata supplies counts without scanning facts. Never query arbitrary browser-supplied names.
    rows = sum(pq.ParquetFile(path).metadata.num_rows for path in _files(table))
    expressions = [f"NULL AS {_identifier(c)}" if masked(table, c) else _identifier(c) for c in columns]
    with connect() as con:
        types = {
            row[0]: row[1]
            for row in con.execute(
                f"DESCRIBE SELECT {', '.join(_identifier(c) for c in columns)} FROM {_identifier(table)}"
            ).fetchall()
        }
        frame = con.execute(f"SELECT {', '.join(expressions)} FROM {_identifier(table)} LIMIT {DICTIONARY_ROWS}").df()
    values = json.loads(frame.to_json(orient="records", date_format="iso"))
    dictionary = []
    for col in columns:
        info = next(
            c for t in _overview(version)["tables"] if t["name"] == table for c in t["columns"] if c["name"] == col
        )
        unique = list(dict.fromkeys(row[col] for row in values if row[col] is not None))
        identifier = col in schema.identifier_columns or info["key"] != "ordinary" or col.endswith("_id")
        limit = 3 if identifier else 16
        complete = (rows <= DICTIONARY_ROWS and len(unique) <= limit) or types[col] == "BOOLEAN" and len(unique) == 2
        complete = complete and not identifier and not info["masked"]
        numeric_date = any(token in types[col] for token in ("INT", "DECIMAL", "DOUBLE", "FLOAT", "DATE", "TIMESTAMP"))
        dictionary.append(
            {
                **info,
                "type": types[col],
                "values": unique[:limit],
                "complete": complete,
                "value_kind": "masked" if info["masked"] else "all non-null values" if complete else "sample values",
                "more_values": len(unique) > limit or rows > DICTIONARY_ROWS,
                "range": [min(unique), max(unique)] if unique and numeric_date and not identifier else None,
                "range_scope": "table" if rows <= DICTIONARY_ROWS else "sample",
            }
        )
    if snapshot_signature() != version:
        raise ValueError("The dataset changed during preview; please reopen it.")
    return to_json(
        {
            "version": version,
            "table": table,
            "rows": values[:SAMPLE_ROWS],
            "row_count": rows,
            "column_count": len(columns),
            "dictionary": dictionary,
            "sampled_rows": min(rows, DICTIONARY_ROWS),
        }
    )
