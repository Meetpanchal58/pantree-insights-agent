"""Data catalog: what every table stores and what every column means, holds and looks like.

Generated from the data and schema.yaml by `pantree-catalog` and committed as semantic/catalog.yaml, so it can be
reviewed like code. For each column:

- meaning: from schema.yaml (every column must have one; tests enforce it) and the entity it describes
  ("seller rating" vs "product review rating": shared column names are told apart by their table)
- role: id | category | text | measure | date | flag
- values: for category columns (text with at most MAX_VALUES = 300 distinct values) every distinct value, most
  frequent first, so filters use exact stored strings ("COD", "Tier 3", "Footwear")
- format / structure: for anything with more distinct values only its shape, never the values: identifiers and codes
  as a regular expression ("OD\\d{9}", "\\d{6}"), names and free text as length and character classes
- range: for numbers (min / median / max) and dates (first / last)
- also_in: other tables with a column of the same name, with what it means there

The agent sees this for every table in its context (catalog.describe); the understanding step uses the value lists
to find which column a word refers to.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import duckdb
import yaml

from .config import get_settings
from .profile import Profile, build_profile
from .semantic import Schema, load_schema

MAX_VALUES = 300              # a text column with more distinct values keeps only its structure (names, codes, ...)
INLINE_VALUES = 10            # values listed inline in prompts; the rest are found through the value search
FREE_TEXT = ("_text", "description", "title", "image_url", "search_query", "transcript")


def _role(kind: str, column: str, distinct: int, id_format) -> str:
    if column.endswith("_id") or id_format is not None:
        return "id"
    if kind == "boolean":
        return "flag"
    if kind == "timestamp":
        return "date"
    if kind in ("number", "integer"):
        return "measure"
    if any(t in column for t in FREE_TEXT) or distinct > MAX_VALUES:
        return "text"
    return "category"


def _structure(con, table: str, column: str, lo: int, hi: int) -> str:
    """Shape of a free-text column without its values: length range and the kinds of characters used."""
    kinds = []
    classes = (("letters", "[A-Za-z]"), ("digits", "[0-9]"), ("spaces", " "), ("punctuation", "[^A-Za-z0-9 ]"))
    for name, pattern in classes:
        found = con.execute(f'SELECT COUNT(*) FILTER (WHERE regexp_matches("{column}", \'{pattern}\')) > 0 '
                            f'FROM {table}').fetchone()[0]
        if found:
            kinds.append(name)
    return f"free text, {lo}-{hi} characters ({', '.join(kinds)})"


def build_catalog(con: duckdb.DuckDBPyConnection, schema: Schema | None = None, profile: Profile | None = None) -> dict:
    schema = schema or load_schema()
    profile = profile or build_profile(con, schema)
    by_name: dict[str, list[tuple[str, str]]] = {}
    for t, info in schema.tables.items():
        for c, d in info.columns.items():
            by_name.setdefault(c, []).append((t, d.split(" - ", 1)[1] if " - " in d else ""))
    out = {}
    for t, info in schema.tables.items():
        columns = {}
        ids = [c for c in info.columns if c.endswith("_id")]
        key = (ids[0], profile.tables[t].columns[ids[0]].distinct) \
            if ids and profile.tables[t].columns[ids[0]].distinct == profile.tables[t].rows else None
        for c, d in info.columns.items():
            kind, meaning = (d.split(" - ", 1) + [""])[:2]
            kind = kind.strip()
            prof = profile.tables[t].columns[c]
            role = _role(kind, c, prof.distinct, prof.id_format)
            entry: dict = {"meaning": meaning, "type": kind, "role": role, "distinct": prof.distinct}
            if prof.nulls:
                entry["empty"] = round(prof.nulls, 3)
            if role == "category":
                rows = con.execute(f'SELECT "{c}", COUNT(*) AS n FROM {t} WHERE "{c}" IS NOT NULL '
                                   f'GROUP BY 1 ORDER BY n DESC, 1').fetchall()
                entry["values"] = [str(v) for v, _ in rows]
            elif role == "id" and prof.id_format is not None:
                entry["format"] = prof.id_format.regex
            elif role == "text" and kind == "text":       # fixed-length digit codes (pincode, barcode) are codes
                n, digits, lo, hi = con.execute(
                    f'SELECT COUNT("{c}"), COUNT(*) FILTER (WHERE regexp_full_match("{c}", \'[0-9]+\')), '
                    f'MIN(length("{c}")), MAX(length("{c}")) FROM {t}').fetchone()
                if n and digits == n:
                    entry["role"] = "id"
                    entry["format"] = f"\\d{{{lo}}}" if lo == hi else f"\\d{{{lo},{hi}}}"
                elif n:
                    entry["structure"] = _structure(con, t, c, lo, hi)
            elif role in ("measure", "date") and prof.min is not None:
                entry["range"] = [prof.min, prof.median, prof.max] if role == "measure" else [prof.min, prof.max]
            if kind == "text" and ("name" in c or c == "title") and key and 0.5 * key[1] <= prof.distinct < key[1]:
                entry["label_of"] = key[0]                 # looks like a key, but several IDs share a name
            others = [f"{o} ({m or o})" for o, m in by_name[c] if o != t]
            if others and not c.endswith("_id"):
                entry["also_in"] = others
            columns[c] = entry
        out[t] = {"entity": info.entity, "grain": info.grain, "description": info.description,
                  "rows": profile.tables[t].rows, "columns": columns}
    return out


def write_catalog(catalog: dict, path: Path) -> None:
    header = ("# Generated by `pantree-catalog` from the data and schema.yaml - do not edit by hand.\n"
              "# Meanings come from schema.yaml; values, formats and ranges from the full shop.\n")
    path.write_text(header + yaml.safe_dump({"tables": catalog}, sort_keys=False, allow_unicode=True, width=120))


@lru_cache
def load_catalog(folder: Path | None = None) -> dict:
    path = (folder or get_settings().semantic_dir) / "catalog.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))["tables"] if path.exists() else {}


def value_columns(catalog: dict | None = None) -> dict[str, list[str]]:
    """Every catalogued category value -> the columns it is stored in ('Mumbai' -> [sales.ship_city, ...])."""
    out: dict[str, list[str]] = {}
    for t, info in (catalog if catalog is not None else load_catalog()).items():
        for c, e in info["columns"].items():
            for v in e.get("values", []):
                out.setdefault(v, []).append(f"{t}.{c}")
    return out


def label_columns(catalog: dict | None = None) -> dict[str, str]:
    """'table.column' -> id column, for names that several IDs share (grouping by them merges rows)."""
    return {f"{t}.{c}": e["label_of"] for t, info in (catalog if catalog is not None else load_catalog()).items()
            for c, e in info["columns"].items() if e.get("label_of")}


def describe(tables: list[str], catalog: dict | None = None, schema: Schema | None = None) -> str:
    """Tables for prompts: what a row is, and for each column its meaning plus values, format or range."""
    catalog = catalog if catalog is not None else load_catalog()
    if not catalog:                                   # no catalog yet: fall back to schema.yaml text
        return (schema or load_schema()).describe(tables)
    blocks = []
    for t in tables:
        info = catalog[t]
        lines = [f"TABLE {t}: one row = {info['entity']} ({info['grain']}). {info['description']}"]
        for c, e in info["columns"].items():
            if "not for analysis" in e["meaning"]:
                continue
            text = f"  - {c} ({e['type']}): {e['meaning']}"
            listed = e.get("values") and all(v in e["meaning"] for v in e["values"])   # the meaning already names them
            if e.get("values") and not listed:
                vals = e["values"]
                shown = ", ".join(f"'{v}'" for v in vals[:INLINE_VALUES])
                text += f". Values: {shown}" + (f" ... ({len(vals)} in total; exact values are in the value list)"
                                               if len(vals) > INLINE_VALUES else "")
            elif e.get("format"):
                text += f". Format: {e['format']}"
            elif e.get("label_of"):
                text += (f". NOT unique: several {e['label_of']} share one {c} ({e['distinct']} names), so group by "
                         f"{e['label_of']} and show {c} as its label")
            elif e.get("structure"):
                shape = e["structure"]
                text += f". {shape[0].upper()}{shape[1:]}; {e['distinct']} distinct values (not listed)"
            elif e.get("range"):
                parts = [str(round(x, 2)) if isinstance(x, float) else str(x) for x in e["range"]]
                text += f". Range: {' / '.join(parts)}"
            if e.get("also_in") and e["role"] in ("category", "measure"):    # where the same name means something else
                text += f". Same name in: {'; '.join(e['also_in'])}"
            lines.append(text)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def main() -> None:
    """pantree-catalog: (re)generate semantic/catalog.yaml from the configured shop (use the full shop)."""
    from .db import connect

    settings = get_settings()
    catalog = build_catalog(connect(settings.shop_dir))
    path = settings.semantic_dir / "catalog.yaml"
    write_catalog(catalog, path)
    cols = sum(len(t["columns"]) for t in catalog.values())
    values = sum(len(e.get("values", [])) for t in catalog.values() for e in t["columns"].values())
    print(f"wrote {path}: {len(catalog)} tables, {cols} columns, {values} category values")
