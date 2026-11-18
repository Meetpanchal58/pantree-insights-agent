"""Reproducible inputs and truthful timing summaries, without calling a provider."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
from provenance import cases_digest, data_digest, latency, stage_seconds  # noqa: E402


def test_data_fingerprint_detects_replaced_and_added_files(tmp_path):
    (tmp_path / "manifest.json").write_text('{"last_day":"2026-11-14"}')
    data = tmp_path / "sales.parquet"
    data.write_bytes(b"old contents")
    before = data_digest(tmp_path)
    data.write_bytes(b"new contents")
    assert data_digest(tmp_path) != before
    before = data_digest(tmp_path)
    (tmp_path / "more.parquet").write_bytes(b"new partition")
    assert data_digest(tmp_path) != before


def test_cases_fingerprint_includes_gold_and_history():
    item = {"id": "q", "question": "q", "gold_sql": "SELECT 1", "history": "earlier"}
    assert cases_digest([item]) != cases_digest([item | {"gold_sql": "SELECT 2"}])
    assert cases_digest([item]) != cases_digest([item | {"history": "changed"}])


def test_timing_keeps_retries_and_missing_stages_distinct():
    events = [{"type": "step", "step": "interpret", "status": status, "detail": {"seconds": seconds}}
              for status, seconds in [("started", 99), ("needs_input", 2), ("done", 3)]]
    assert stage_seconds(events) == {"interpret": 5}
    assert latency([])["p95"] is None
    assert latency([1, 2, 3, 4, 100]) == {"n": 5, "p50": 3, "p95": 100, "max": 100}


def test_rescore_rejects_unpinned_legacy_reports(tmp_path, monkeypatch):
    import run_eval

    report = tmp_path / "old.json"
    report.write_text('[{"status":"done"}]')
    monkeypatch.setattr(run_eval, "data_digest", lambda path: "current")
    with pytest.raises(ValueError, match="fingerprint"):
        run_eval.rescore(report)
    assert report.read_text() == '[{"status":"done"}]'
