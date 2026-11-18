"""Offline regression checks. Synthetic vectors test mechanics, not real embedding accuracy."""
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from pantree_insights import retrieval
from pantree_insights.config import ROOT, Settings
from pantree_insights.knowledge import Record, build_records, fingerprint
from pantree_insights.retrieval import KnowledgeSearch, Retrieved, Retriever, build_index, retrieval_query


class Encoder:
    signature = "test-model"

    def query(self, text, *, cached=True):
        return np.array([1., 0.])

    def encode(self, texts):
        return np.array([[1., 0.]] * len(texts))


@pytest.fixture
def records():
    return [Record("metric:orders", "metric", "Order count", "COUNT(DISTINCT order_id)", "metrics",
                   ("table:sales",)),
            Record("table:sales", "table", "Sales", "Sales lines, not distinct orders", "schema"),
            Record("column:sales.city", "column", "Destination city", "Mumbai Pune", "schema", ("table:sales",))]


def test_knowledge_sources_are_deterministic_and_dependencies_resolve():
    records = build_records()
    assert records == build_records()
    assert len({r.id for r in records}) == len(records)
    assert all(set(r.dependencies) <= {x.id for x in records} for r in records)
    assert {r.kind for r in records} >= {"table", "metric", "column", "concept", "join", "term", "analysis"}
    assert all(r.source.startswith(("semantic/", "src/pantree_insights/templates.py")) for r in records)
    assert not any("customer_pii" in r.id for r in records)
    orders = next(r for r in records if r.id == "table:sales")
    assert "sales.shipped_at" in orders.text and "sales.line_type = 'Sale'" in orders.text
    changed = [replace(records[0], text="changed rule"), *records[1:]]
    assert fingerprint(changed) != fingerprint(records)


@pytest.mark.parametrize("mode", ["keyword", "vector", "hybrid"])
def test_named_metric_and_complete_dependencies_survive_column_competition(records, mode):
    search = Retriever(records, np.array([[0., 1.], [0., 1.], [1., 0.]]), Encoder())
    result = search.retrieve("Count orders for Mumbai Pune", mode=mode, top_k=1)
    assert result.ranked_ids == ["metric:orders"]
    assert {r.id for r in result.records} == {"metric:orders", "table:sales"}


def test_budget_cannot_partially_include_a_required_bundle(records):
    search = Retriever(records, np.ones((3, 2)), Encoder())
    result = search.retrieve("orders", top_k=1, max_chars=len(records[0].render()) + 2)
    assert result.overflow and result.records == []


def test_vector_search_finds_semantic_candidate_without_keyword_overlap(records):
    search = Retriever(records, np.array([[1., 0.], [0., 1.], [0., 1.]]), Encoder())
    assert search.retrieve("transactions", mode="keyword", top_k=1).ranked_ids == []
    assert search.retrieve("transactions", mode="vector", top_k=1).ranked_ids == ["metric:orders"]


def test_parallel_queries_do_not_mix_evidence(records):
    search = Retriever(records, np.array([[1., 0.], [0., 1.], [0., 1.]]), Encoder())
    queries = ["orders", "Mumbai Pune"] * 12
    expected = [search.retrieve(q, mode="keyword", top_k=1).ranked_ids for q in queries]
    with ThreadPoolExecutor(max_workers=4) as pool:
        actual = list(pool.map(lambda q: search.retrieve(q, mode="keyword", top_k=1).ranked_ids, queries))
    assert actual == expected


@pytest.mark.parametrize("vectors", [np.ones((2, 2)), np.zeros((3, 2)), np.full((3, 2), np.nan)])
def test_corrupt_embedding_matrix_is_rejected(records, vectors):
    with pytest.raises(ValueError):
        Retriever(records, vectors, Encoder())


def test_stale_or_wrong_model_index_is_rejected_before_serving(records, tmp_path, monkeypatch):
    path = tmp_path / "index.npz"
    build_index(records, Encoder(), path)
    monkeypatch.setattr(retrieval, "Encoder", lambda _: Encoder())
    assert Retriever.load(records, path, tmp_path).records == records
    with pytest.raises(ValueError, match="stale"):
        Retriever.load([replace(records[0], text="new metric"), *records[1:]], path, tmp_path)
    wrong = Encoder()
    wrong.signature = "different weights"
    monkeypatch.setattr(retrieval, "Encoder", lambda _: wrong)
    with pytest.raises(ValueError, match="model does not match"):
        Retriever.load(records, path, tmp_path)


def settings(tmp_path, mode="hybrid"):
    return Settings(_env_file=None, retrieval_mode=mode, retrieval_index=tmp_path / "missing.npz",
                    retrieval_model_dir=tmp_path / "missing-model", semantic_dir=ROOT / "semantic")


def test_missing_index_falls_back_without_loading_or_downloading_a_model(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Model loading/download is forbidden with no index")
    monkeypatch.setattr(retrieval, "Encoder", forbidden)
    service = KnowledgeSearch(settings(tmp_path))
    context, trace = service.context("orders", "private project", {})
    assert context is None and trace["used_context"] == "full"
    assert "FileNotFoundError" in trace["fallback"]


@pytest.mark.parametrize("mode,overflow,used", [("shadow", False, "full"), ("hybrid", True, "full"),
                                               ("hybrid", False, "hybrid")])
def test_shadow_and_overflow_cannot_replace_catalog(records, tmp_path, mode, overflow, used):
    service = KnowledgeSearch(settings(tmp_path, mode))
    result = Retrieved([records[0].id], records, .02, .01, overflow, "hybrid")
    service.retriever = SimpleNamespace(retrieve=lambda *a, **k: result, records=records, encoder=Encoder())
    service.fallback = None
    context, trace = service.context("orders", "", {})
    assert trace["used_context"] == used
    assert (context is None) == (used == "full")
    assert trace["record_ids"] and trace["retrieval_seconds"] == .02


def test_failed_inference_falls_back_without_leaking_query_to_trace(tmp_path):
    service = KnowledgeSearch(settings(tmp_path))

    def fail(*args, **kwargs):
        raise RuntimeError("private query must not enter error trace")
    service.retriever = SimpleNamespace(retrieve=fail)
    context, trace = service.context("orders", "private query", {})
    assert context is None and "RuntimeError" in trace["fallback"]
    assert "private query" not in json.dumps(trace)


def test_followup_query_keeps_question_answers_and_recent_context_bounded():
    query = retrieval_query("Break them down", "x" * 5000 + "ADIDAS Nike", {"Period?": "last 30 days"})
    assert query.startswith("Break them down") and "ADIDAS Nike" in query and "last 30 days" in query
    assert len(query) < 4000
    assert retrieval_query("orders") == "orders"


def test_real_encoder_optional_smoke():
    # Main accuracy/latency evidence comes from retrieval_eval.py, not this tiny smoke test.
    if not (retrieval.DEFAULT_MODEL_DIR / "onnx/model.onnx").exists():
        pytest.skip("run pantree-knowledge build --download-model to test the local encoder")
    encoder = retrieval.Encoder(retrieval.DEFAULT_MODEL_DIR)
    vectors = encoder.encode(["How many orders did customers place?", "Count purchases", "Warehouse shelf height"])
    assert vectors.shape == (3, 384)
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1, atol=1e-5)
    assert vectors[0] @ vectors[1] > vectors[0] @ vectors[2]
    assert np.allclose(encoder.query("Count purchases"), encoder.query("Count purchases", cached=False))
