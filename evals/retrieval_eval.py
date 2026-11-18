"""Compare real keyword, vector and hybrid retrieval with identical evidence/context budgets; no LLM calls."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import math
import os
import platform
import random
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from provenance import cases_digest, digest_files, latency  # noqa: E402
from run_eval import load_suite  # noqa: E402

from pantree_insights.knowledge import build_records, fingerprint, join_id  # noqa: E402
from pantree_insights.retrieval import DEFAULT_INDEX, DEFAULT_MODEL_DIR, MODEL, REVISION, Retriever, retrieval_query  # noqa: E402


def required(item: dict) -> set[str]:
    return {*(f"table:{t}" for t in item.get("required_tables", [])),
            *(f"metric:{m}" for m in item.get("required_metrics", [])),
            *(join_id(j) for j in item.get("required_joins", []))}


def grade(result, expected: set[str]) -> dict:
    returned = {r.id for r in result.records}
    ranked = result.ranked_ids[:10]
    dcg = sum(1 / math.log2(i + 2) for i, id in enumerate(ranked) if id in expected)
    ideal = sum(1 / math.log2(i + 2) for i in range(min(len(expected), 10)))
    return {"required_recall": len(expected & returned) / len(expected),
            "complete_evidence": expected <= returned, "missing": sorted(expected - returned),
            "ndcg10_direct_labels": dcg / ideal, "overflow": result.overflow,
            "context_chars": len(result.context()), "seconds": result.seconds,
            "embedding_seconds": result.embedding_seconds,
            "ranked_ids": result.ranked_ids, "returned_ids": sorted(returned)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--suite", choices=["flow", "holdout", "coverage", "all"], default="all")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--top-k", type=int, default=12)
    parser.add_argument("--max-chars", type=int, default=24000)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "evals" / "reports")
    parser.add_argument("--gate", action="store_true", help="require hybrid recall >=99%, complete >=95%, p95 <=500ms")
    args = parser.parse_args()
    if min(args.repeats, args.top_k, args.max_chars) < 1:
        parser.error("Repeats and budgets must be positive")
    if args.gate and args.suite not in ("holdout", "all"):
        parser.error("The release gate requires the holdout suite (or all), not development cases alone")
    records = build_records()
    items = [i for i in load_suite(args.suite) if i["set"] == "answer" and required(i)]
    if not items:
        parser.error("No annotated retrieval cases")
    ids = {r.id for r in records}
    for item in items:
        if missing := required(item) - ids:
            parser.error(f"Unknown required knowledge labels in {item['id']}: {sorted(missing)}")
    started = time.perf_counter()
    retriever = Retriever.load(records, args.index, args.model_dir)
    startup = time.perf_counter() - started
    # Warm the ONNX session once. Query embedding cache is disabled for every measured call.
    first_query = time.perf_counter()
    retriever.encoder.query("business data", cached=False)
    first_query_seconds = time.perf_counter() - first_query
    rows = []
    jobs = [(mode, repeat, item) for mode in ("keyword", "vector", "hybrid")
            for repeat in range(args.repeats) for item in items]
    random.Random(20261118).shuffle(jobs)
    for mode, repeat, item in jobs:
        query = retrieval_query(item["question"], item.get("history", ""))
        result = retriever.retrieve(query, mode=mode, top_k=args.top_k, max_chars=args.max_chars, cached=False)
        rows.append({"suite": item["suite"], "id": item["id"], "repeat": repeat, "mode": mode,
                     "question": item["question"], **grade(result, required(item))})
    summary = {}
    for suite in sorted({r["suite"] for r in rows} | {"all"}):
        for mode in ("keyword", "vector", "hybrid"):
            subset = [r for r in rows if r["mode"] == mode and (suite == "all" or r["suite"] == suite)]
            if not subset:
                continue
            summary[f"{suite}/{mode}"] = {
                "runs": len(subset), "questions": len(subset) // args.repeats,
                "required_recall": statistics.mean(r["required_recall"] for r in subset),
                "complete_evidence": statistics.mean(r["complete_evidence"] for r in subset),
                "ndcg10_direct_labels": statistics.mean(r["ndcg10_direct_labels"] for r in subset),
                "overflow_rate": statistics.mean(r["overflow"] for r in subset),
                "latency_seconds": latency([r["seconds"] for r in subset]),
                "mean_context_chars": statistics.mean(r["context_chars"] for r in subset)}
    files = [p for folder in ("src", "semantic", "evals") for p in (ROOT / folder).rglob("*")
             if p.suffix in (".py", ".yaml") and "reports" not in p.parts]
    files += [ROOT / "pyproject.toml", ROOT / "uv.lock"]
    report = {"kind": "retrieval_only", "source_sha256": digest_files(ROOT, files),
              "packages": {p: importlib.metadata.version(p) for p in ("onnxruntime", "tokenizers", "numpy")},
              "platform": platform.platform(), "cpu_count": os.cpu_count(), "onnx_threads": 1,
              "protocol": "explicit metric-name anchors + top-k ranking + whole dependency bundles; max 256 tokens",
              "first_query_embedding_seconds": first_query_seconds, "model": MODEL, "model_revision": REVISION,
              "encoder_sha256": retriever.encoder.signature, "knowledge_sha256": fingerprint(records),
              "cases_sha256": cases_digest(items), "repeats": args.repeats, "top_k": args.top_k,
              "max_chars": args.max_chars, "startup_seconds": startup, "summary": summary, "rows": rows,
              "code_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
              "working_tree_dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], text=True)),
              "cache_mode": "warm_process; query_embedding_cache_disabled", "provider_calls": 0}
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stem = args.output_dir / f"retrieval_{datetime.now(UTC):%Y%m%dT%H%M%S_%fZ}"
    stem.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n")
    lines = ["# Retrieval comparison", "",
             "| Suite / method | Recall | Complete evidence | p50 / p95 (ms) | Overflow |",
             "|---|---:|---:|---:|---:|"]
    for key, row in summary.items():
        t = row["latency_seconds"]
        lines.append(f"| {key} | {row['required_recall']:.1%} | {row['complete_evidence']:.1%} | "
                     f"{1000*t['p50']:.1f} / {1000*t['p95']:.1f} | {row['overflow_rate']:.1%} |")
    lines += ["", "Scores cover annotated tables, metrics and joins; column-level intent, answer accuracy and "
              "business insights are not measured. Unlabelled concepts may be relevant. nDCG in JSON uses direct "
              "labels only; dependency expansion is measured separately. "
              "Repeats measure timing, not independent questions.",
              "Every method uses the same explicit metric-name anchors, seed count and character budget.",
              "Oversized bundles are omitted whole; fallback to the full catalog is not counted as retrieval success.",
              f"Model/index startup: {startup:.3f}s, excluded from warm timings. No provider calls."]
    text = "\n".join(lines)
    stem.with_suffix(".md").write_text(text + "\n")
    print(text + f"\n\nReports: {stem}.json / .md")
    check = summary.get("holdout/hybrid", summary["all/hybrid"])
    if args.gate and (check["required_recall"] < .99 or check["complete_evidence"] < .95
                      or check["overflow_rate"] > 0 or check["latency_seconds"]["p95"] > .5):
        sys.exit(1)


if __name__ == "__main__":
    main()
