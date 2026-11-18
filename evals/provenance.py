"""Versioned evaluation inputs and timing summaries. Never reads secrets or model credentials."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import platform
import statistics
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCORER_VERSION = "strict-v1"


def cases_digest(items: list[dict]) -> str:
    return hashlib.sha256(json.dumps(items, sort_keys=True, default=str).encode()).hexdigest()


def digest_files(root: Path, paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        file_digest = hashlib.sha256()
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                file_digest.update(block)
        digest.update(file_digest.digest())
    return digest.hexdigest()


def data_digest(shop: Path) -> str:
    paths = [shop / "manifest.json", *shop.rglob("*.parquet")]
    if len(paths) == 1:
        raise ValueError("No parquet data found for the evaluation snapshot")
    return digest_files(shop, paths)


def provenance(items: list[dict], shop: Path) -> dict:
    from pantree_insights.config import get_settings

    def git(*args):
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()

    settings = get_settings()
    source = [p for folder in ("src", "semantic", "prompts", "evals") for p in (ROOT / folder).rglob("*")
              if p.suffix in (".py", ".yaml") and "reports" not in p.parts]
    source += [ROOT / "pyproject.toml", ROOT / "uv.lock"]
    versions = {p: importlib.metadata.version(p) for p in ("duckdb", "pandas", "sqlglot", "langgraph")}
    return {"code_revision": git("rev-parse", "HEAD"), "working_tree_dirty": bool(git("status", "--porcelain")),
            "source_sha256": digest_files(ROOT, source), "data_sha256": data_digest(shop),
            "data_manifest": json.loads((shop / "manifest.json").read_text()),
            "cases_sha256": cases_digest(items),
            "scorer_version": SCORER_VERSION, "python": platform.python_version(), "packages": versions,
            "settings": {name: getattr(settings, name) for name in
                         ("model_main", "model_light", "temperature", "contrast_check", "query_timeout_s", "max_rows",
                          "retrieval_mode", "retrieval_top_k", "retrieval_max_chars")}}


def latency(values: list[float]) -> dict:
    """Nearest-rank p95; report sample count so small experiments are not mistaken for load tests."""
    values = sorted(values)
    if not values:
        return {"n": 0, "p50": None, "p95": None, "max": None}
    return {"n": len(values), "p50": statistics.median(values),
            "p95": values[math.ceil(0.95 * len(values)) - 1], "max": values[-1]}


def stage_seconds(events: list[dict]) -> dict:
    result = {}
    for e in events:
        if e.get("type") == "step" and e.get("status") != "started":
            seconds = e.get("detail", {}).get("seconds")
            if seconds is not None:
                result[e["step"]] = result.get(e["step"], 0) + seconds
    return result
