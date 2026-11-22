"""Data access: download the simulator's shop and query it through locked-down DuckDB views.

    pantree-data download              # DATA_SIZE from settings (default: full)
    pantree-data download --size small
    pantree-data info

Only the tables in semantic/schema.yaml are exposed, without blocked columns. After the views are
created, the connection can read nothing but the shop's folder, cannot write files, install
extensions or change its own settings.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import threading
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pandas as pd

from .config import get_settings
from .semantic import load_schema

HF_REPO = "Meet58/ecommerce-simulator"


# ----------------------------------------------------------------------------- download
def download(size: str | None = None, data_dir: Path | None = None) -> Path:
    """Download one shop size from Hugging Face, skipping blocked and internal tables."""
    from huggingface_hub import snapshot_download

    settings = get_settings()
    size = size or settings.data_size
    data_dir = data_dir or settings.data_dir
    schema = load_schema()
    allow = [f"{size}/manifest.json"]
    for table in schema.tables:
        allow += [f"{size}/{table}.parquet", f"{size}/{table}/month=*/part.parquet"]
    snapshot_download(HF_REPO, repo_type="dataset", allow_patterns=allow, local_dir=data_dir, max_workers=8)
    return data_dir / size


def manifest(shop_dir: Path | None = None) -> dict:
    """The shop's metadata: size, first and last simulated day, ..."""
    shop_dir = shop_dir or get_settings().shop_dir
    return json.loads((shop_dir / "manifest.json").read_text())


def snapshot_signature() -> str:
    """Identity of the published data manifest. Publishers must update it after a data refresh."""
    path = get_settings().shop_dir / "manifest.json"
    stat = path.stat()
    definitions = b"".join(p.name.encode() + p.read_bytes()
                            for p in sorted(get_settings().semantic_dir.glob("*.yaml")))
    return hashlib.sha256(str(path.resolve()).encode() + path.read_bytes() + definitions
                          + get_settings().prompts_file.read_bytes() + str(stat.st_mtime_ns).encode()).hexdigest()


# ----------------------------------------------------------------------------- connection
def _source(shop_dir: Path, table: str) -> str:
    single = shop_dir / f"{table}.parquet"
    if single.exists():
        return f"read_parquet('{single.as_posix()}')"
    monthly = shop_dir / table
    if monthly.is_dir():
        return f"read_parquet('{monthly.as_posix()}/month=*/part.parquet', hive_partitioning=false)"
    raise FileNotFoundError(f"{table} not found in {shop_dir}; run `pantree-data download`")


def connect(shop_dir: Path | None = None, threads: int | None = None) -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB with one view per allowed table, then locked down."""
    shop_dir = (shop_dir or get_settings().shop_dir).resolve()
    schema = load_schema()
    con = duckdb.connect(config={"threads": threads} if threads else {})
    for table, info in schema.tables.items():
        columns = ", ".join(f'"{c}"' for c in info.columns)
        con.execute(f"CREATE VIEW {table} AS SELECT {columns} FROM {_source(shop_dir, table)}")
    con.execute(f"SET allowed_directories = ['{shop_dir.as_posix()}/']")
    con.execute("SET enable_external_access = false")
    con.execute("SET lock_configuration = true")
    return con


# ----------------------------------------------------------------------------- running queries
@dataclass
class QueryResult:
    sql: str
    frame: pd.DataFrame
    truncated: bool          # more rows existed than max_rows
    seconds: float


class QueryTimeout(RuntimeError):
    pass


def run_query(con: duckdb.DuckDBPyConnection, sql: str, max_rows: int | None = None,
              timeout_s: float | None = None) -> QueryResult:
    """Run one already-checked SELECT. Returns at most max_rows rows; cancels after timeout_s."""
    import time

    settings = get_settings()
    max_rows = max_rows or settings.max_rows
    timeout_s = timeout_s or settings.query_timeout_s
    cursor = con.cursor()            # own cursor: interrupting it never touches other queries
    timer = threading.Timer(timeout_s, cursor.interrupt)
    started = time.perf_counter()
    timer.start()
    try:
        cursor.execute(sql)
        columns = [d[0] for d in cursor.description]
        rows = cursor.fetchmany(max_rows + 1)
    except duckdb.InterruptException as error:
        raise QueryTimeout(f"query took longer than {timeout_s:.0f}s and was cancelled") from error
    finally:
        timer.cancel()
        cursor.close()
    frame = pd.DataFrame(rows[:max_rows], columns=columns)
    return QueryResult(sql, frame, len(rows) > max_rows, time.perf_counter() - started)


# ----------------------------------------------------------------------------- command line
def main() -> None:
    parser = argparse.ArgumentParser(prog="pantree-data")
    sub = parser.add_subparsers(dest="command", required=True)
    dl = sub.add_parser("download", help="download the shop from Hugging Face")
    dl.add_argument("--size", choices=["small", "full", "large"])
    sub.add_parser("info", help="show the downloaded shop and table sizes")
    args = parser.parse_args()
    if args.command == "download":
        path = download(args.size)
        print(f"Downloaded to {path}: {manifest(path)}")
    else:
        con = connect()
        print(manifest())
        for table in load_schema().tables:
            print(f"{table:24s} {con.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]:>12,}")


if __name__ == "__main__":
    main()
