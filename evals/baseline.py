"""Free, reproducible compiler baseline. Does NOT measure model understanding or hybrid retrieval.

    DATA_SIZE=small uv run python evals/baseline.py --repeats 3
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from provenance import data_digest, latency, provenance  # noqa: E402
from run_eval import load_suite  # noqa: E402
from spec_eval import check, load_gold_specs  # noqa: E402

from pantree_insights.config import get_settings  # noqa: E402
from pantree_insights.spec import default_catalog  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=["all", "regression", "holdout", "coverage", "flow"], default="all")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "evals" / "reports")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    items = [i for i in load_suite(args.suite) if i["set"] == "answer"]
    shop = get_settings().shop_dir
    inputs = provenance(items, shop)
    started = time.perf_counter()
    cat, specs = default_catalog(), load_gold_specs()
    setup_seconds = time.perf_counter() - started
    rows = []
    for repeat in range(args.repeats):
        for item in items:
            key = f"{item['suite']}/{item['id']}"
            result = check(item, specs[key], cat)
            rows.append({"id": key, "repeat": repeat, **result})
        print(f"Finished pass {repeat + 1}/{args.repeats}: {sum(r['ok'] for r in rows)}/{len(rows)} matched",
              flush=True)
    unchanged = data_digest(shop) == inputs["data_sha256"]
    report = {"kind": "compiler_only", "created_at": datetime.now(UTC).isoformat(), "inputs": inputs,
              "repeats": args.repeats, "cases": len(items), "snapshot_verified": unchanged,
              "setup_seconds": setup_seconds, "cache_mode": "shared_connection; no_agent_sql_result_cache",
              "model_accuracy": None, "retrieval_accuracy": None, "retrieval_seconds": None, "model_cost_usd": 0,
              "query_latency": latency([r["query_seconds"] for r in rows if "query_seconds" in r]),
              "compile_latency": latency([r["compile_seconds"] for r in rows if "compile_seconds" in r]),
              "rows": rows}
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stem = args.output_dir / f"baseline_{datetime.now(UTC):%Y%m%dT%H%M%S_%fZ}"
    stem.with_suffix(".json").write_text(json.dumps(report, indent=2, default=str) + "\n")
    timing = report["query_latency"]
    displayed = "/".join(f"{timing[k]:.4f}" if timing[k] is not None else "unavailable"
                         for k in ("p50", "p95", "max"))
    text = (f"# Compiler baseline\n\n{sum(r['ok'] for r in rows)}/{len(rows)} results matched gold SQL across "
            f"{len(items)} cases and {args.repeats} passes. Snapshot unchanged: {unchanged}.\n\n"
            f"Setup: {setup_seconds:.3f}s. Query p50/p95/max: {displayed}s (n={timing['n']}).\n\n"
            "No model calls. These figures measure compiler and local DuckDB execution only; they do not "
            "measure retrieval, question understanding, full answers, Cloud Run cold starts or concurrent users. "
            "Passes share a connection and OS caches; the first pass is not a controlled cold-start benchmark.\n")
    stem.with_suffix(".md").write_text(text)
    print(text + f"\nReports: {stem}.json / .md")
    if not unchanged or not all(r["ok"] for r in rows):
        sys.exit(1)


if __name__ == "__main__":
    main()
