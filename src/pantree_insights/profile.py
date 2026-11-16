"""Data profile: facts about every table, column and approved join, measured from the data (no LLM).

- columns: type, null share, distinct count; text columns: ID format (e.g. order_id = 'OD' + 9 digits) or the
  number of distinct values; numbers: min / median / max; timestamps: first and last value
- joins: measured cardinality of each approved join (1:1, N:1, 1:N, N:M) and the share of rows on each side
  whose key finds no partner (decides whether an inner join drops rows)

Built once per data snapshot and cached in .cache/ (a few seconds on the full shop).
`pantree-profile` prints it.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path

import duckdb

from .config import ROOT, get_settings
from .semantic import Schema, load_schema

CACHE = ROOT / ".cache"
ID_SHARE = 0.99                     # an ID format must describe at least this share of values


@dataclass
class IdFormat:
    prefix: str                     # 'OD'
    digits: int                     # 9
    suffix: str = ""                # regex for anything after the number (sku: '-[A-Z0-9]+')

    @property
    def regex(self) -> str:
        return f"{re.escape(self.prefix)}\\d{{{self.digits}}}{self.suffix}"

    def example(self) -> str:
        return f"{self.prefix}{'0' * (self.digits - 1)}1"


@dataclass
class ColumnProfile:
    type: str
    nulls: float                    # share of rows that are NULL
    distinct: int
    id_format: IdFormat | None = None
    min: float | str | None = None
    median: float | None = None
    max: float | str | None = None


@dataclass
class TableProfile:
    rows: int
    columns: dict[str, ColumnProfile] = field(default_factory=dict)


@dataclass
class JoinProfile:
    join: str                       # 'sales.product_id = product_master.product_id'
    left_per_key: int               # most left rows sharing one key value
    right_per_key: int
    left_unmatched: float           # share of left rows (non-null key) with no right partner
    right_unmatched: float

    @property
    def cardinality(self) -> str:
        """'N:1' = many left rows per right row (joining right onto left never multiplies left rows)."""
        return f"{'1' if self.left_per_key <= 1 else 'N'}:{'1' if self.right_per_key <= 1 else 'N'}"


@dataclass
class Profile:
    tables: dict[str, TableProfile]
    joins: dict[str, JoinProfile]

    def column(self, qualified: str) -> ColumnProfile:
        table, column = qualified.split(".", 1)
        return self.tables[table].columns[column]

    def id_formats(self) -> dict[str, IdFormat]:
        """'sales.order_id' -> IdFormat for every text column with a single ID format."""
        return {f"{t}.{c}": p.id_format for t, tp in self.tables.items() for c, p in tp.columns.items()
                if p.id_format}


# ----------------------------------------------------------------------------- measuring
_ID_SHAPE = re.compile(r"^([A-Z]{1,5})(\d{3,12})(-[A-Z0-9]+)?$")


def id_format(values: list[str]) -> IdFormat | None:
    """The single 'PREFIX + n digits (+ -SUFFIX)' format shared by >= 99% of the values, else None."""
    shapes: dict[tuple[str, int, bool], int] = {}
    for value in values:
        m = _ID_SHAPE.match(value)
        if m:
            key = (m.group(1), len(m.group(2)), m.group(3) is not None)
            shapes[key] = shapes.get(key, 0) + 1
    if not shapes or not values:
        return None
    (prefix, digits, has_suffix), count = max(shapes.items(), key=lambda kv: kv[1])
    if count / len(values) < ID_SHARE:
        return None
    return IdFormat(prefix, digits, "-[A-Z0-9]+" if has_suffix else "")


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def profile_table(con: duckdb.DuckDBPyConnection, table: str, kinds: dict[str, str]) -> TableProfile:
    rows = con.execute(f"SELECT COUNT(*) FROM {_q(table)}").fetchone()[0]
    out = TableProfile(rows=rows)
    for column, kind in kinds.items():
        c = _q(column)
        nulls, distinct = con.execute(f"SELECT COUNT(*) - COUNT({c}), COUNT(DISTINCT {c}) FROM {_q(table)}").fetchone()
        prof = ColumnProfile(type=kind, nulls=round(nulls / rows, 4) if rows else 0.0, distinct=distinct)
        if kind == "text" and distinct > 50:                      # small code lists are categories, not IDs
            sample = [v for (v,) in con.execute(
                f"SELECT DISTINCT {c} FROM {_q(table)} WHERE {c} IS NOT NULL LIMIT 5000").fetchall()]
            prof.id_format = id_format([str(v) for v in sample])
        elif kind in ("number", "integer"):
            lo, mid, hi = con.execute(f"SELECT MIN({c}), MEDIAN({c}), MAX({c}) FROM {_q(table)}").fetchone()
            prof.min, prof.median, prof.max = (None if v is None else float(v) for v in (lo, mid, hi))
        elif kind == "timestamp":
            lo, hi = con.execute(f"SELECT MIN({c}), MAX({c}) FROM {_q(table)}").fetchone()
            prof.min, prof.max = (None if lo is None else str(lo)[:19]), (None if hi is None else str(hi)[:19])
        out.columns[column] = prof
    return out


def profile_join(con: duckdb.DuckDBPyConnection, join: str, extra: str | None = None) -> JoinProfile:
    """Cardinality of an approved join; with a join rule's `extra` condition, measured with it applied."""
    (lt, lc), (rt, rc) = (side.strip().split(".") for side in join.split("="))
    if extra:
        return _profile_join_with_rule(con, join, lt, lc, rt, rc, extra)
    per_key = "SELECT COALESCE(MAX(n), 0) FROM (SELECT COUNT(*) AS n FROM {t} WHERE {c} IS NOT NULL GROUP BY {c})"
    left_per_key = con.execute(per_key.format(t=_q(lt), c=_q(lc))).fetchone()[0]
    right_per_key = con.execute(per_key.format(t=_q(rt), c=_q(rc))).fetchone()[0]
    unmatched = ("SELECT COUNT(*) FILTER (WHERE r.k IS NULL), COUNT(*) FROM (SELECT {c} AS k FROM {t} "
                 "WHERE {c} IS NOT NULL) l LEFT JOIN (SELECT DISTINCT {c2} AS k FROM {t2}) r ON l.k = r.k")
    miss_l, all_l = con.execute(unmatched.format(t=_q(lt), c=_q(lc), t2=_q(rt), c2=_q(rc))).fetchone()
    miss_r, all_r = con.execute(unmatched.format(t=_q(rt), c=_q(rc), t2=_q(lt), c2=_q(lc))).fetchone()
    return JoinProfile(join, left_per_key, right_per_key, round(miss_l / all_l, 4) if all_l else 0.0,
                       round(miss_r / all_r, 4) if all_r else 0.0)


def _profile_join_with_rule(con, join, lt, lc, rt, rc, extra) -> JoinProfile:
    """Matches per row through the full ON condition (rows get a temporary number; views have no rowid)."""
    on = re.sub(rf"\b{lt}\.", "l.", re.sub(rf"\b{rt}\.", "r.", extra))
    sql = (f"WITH l AS (SELECT *, ROW_NUMBER() OVER () AS __l FROM {_q(lt)} WHERE {_q(lc)} IS NOT NULL), "
           f"r AS (SELECT *, ROW_NUMBER() OVER () AS __r FROM {_q(rt)}), "
           f"m AS (SELECT l.__l, r.__r FROM l LEFT JOIN r ON l.{_q(lc)} = r.{_q(rc)} AND {on}) "
           "SELECT (SELECT MAX(n) FROM (SELECT COUNT(__r) AS n FROM m GROUP BY __l)), "
           "(SELECT COALESCE(MAX(n), 0) FROM (SELECT COUNT(*) AS n FROM m WHERE __r IS NOT NULL GROUP BY __r)), "
           "(SELECT AVG(CASE WHEN n = 0 THEN 1.0 ELSE 0 END) FROM (SELECT COUNT(__r) AS n FROM m GROUP BY __l))")
    right_per_left, left_per_right, unmatched = con.execute(sql).fetchone()
    return JoinProfile(join, left_per_key=left_per_right, right_per_key=right_per_left,
                       left_unmatched=round(unmatched or 0.0, 4), right_unmatched=0.0)


def build_profile(con: duckdb.DuckDBPyConnection, schema: Schema | None = None) -> Profile:
    schema = schema or load_schema()
    kinds = {t: {c: d.split(" - ")[0].strip() for c, d in info.columns.items()} for t, info in schema.tables.items()}
    tables = {t: profile_table(con, t, kinds[t]) for t in schema.tables}
    joins = {j: profile_join(con, j, schema.join_rules[j].extra if j in schema.join_rules else None)
             for j in schema.joins}
    return Profile(tables, joins)


# ----------------------------------------------------------------------------- cache
def _to_json(profile: Profile) -> str:
    return json.dumps(asdict(profile), default=str)


def _from_json(text: str) -> Profile:
    raw = json.loads(text)
    tables = {}
    for t, tp in raw["tables"].items():
        cols = {c: ColumnProfile(**{**p, "id_format": IdFormat(**p["id_format"]) if p["id_format"] else None})
                for c, p in tp["columns"].items()}
        tables[t] = TableProfile(rows=tp["rows"], columns=cols)
    joins = {j: JoinProfile(**jp) for j, jp in raw["joins"].items()}
    return Profile(tables, joins)


def cached_profile(con: duckdb.DuckDBPyConnection, key: str) -> Profile:
    """Profile for one data snapshot; `key` must change when the data or schema changes."""
    path = CACHE / f"profile_{re.sub(r'[^A-Za-z0-9_.-]', '_', key)}.json"
    if path.exists():
        try:
            return _from_json(path.read_text())
        except (ValueError, KeyError, TypeError):
            pass
    profile = build_profile(con)
    CACHE.mkdir(parents=True, exist_ok=True)
    path.write_text(_to_json(profile))
    return profile


def profile_for(con: duckdb.DuckDBPyConnection, info: dict) -> Profile:
    """Cached profile for the data snapshot described by `info` (db.manifest: size, last_day)."""
    schema_text = (get_settings().semantic_dir / "schema.yaml").read_bytes()
    code = Path(__file__).read_bytes()                       # a change in how we measure invalidates the cache
    key = f"{info['size']}_{info['last_day']}_{hashlib.sha1(schema_text + code).hexdigest()[:8]}"
    return cached_profile(con, key)


@lru_cache
def get_profile() -> Profile:
    from .db import connect, manifest

    settings = get_settings()
    return profile_for(connect(settings.shop_dir), manifest(settings.shop_dir))


def main() -> None:
    """pantree-profile: ID formats, join cardinality and unmatched rows."""
    profile = get_profile()
    print("ID formats:")
    for col, fmt in sorted(profile.id_formats().items()):
        print(f"  {col:42s} {fmt.regex:28s} e.g. {fmt.example()}")
    print("\nJoins (cardinality, unmatched left / right):")
    for j in profile.joins.values():
        print(f"  {j.cardinality:4s} {j.left_unmatched:6.1%} / {j.right_unmatched:6.1%}  {j.join}")
