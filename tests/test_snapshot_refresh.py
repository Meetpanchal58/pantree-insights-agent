"""Rebuild metadata on a published manifest change, and don't run SQL with a stale interpretation."""
from types import SimpleNamespace

from pantree_insights import db, graph, retrieval, web


def test_manifest_signature_includes_version_and_location(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "get_settings", lambda: SimpleNamespace(shop_dir=tmp_path))
    path = tmp_path / "manifest.json"
    path.write_text('{"last_day":"2026-11-15"}')
    old = db.snapshot_signature()
    path.write_text('{"last_day":"2026-11-16"}')
    assert db.snapshot_signature() != old


def test_retriever_rebuilds_once_per_snapshot(monkeypatch):
    version = ["one"]
    builds = []
    monkeypatch.setattr(retrieval, "snapshot_signature", lambda: version[0])
    monkeypatch.setattr(retrieval, "connect", lambda *a: None)
    monkeypatch.setattr(retrieval, "Retriever", lambda *a: builds.append(object()) or builds[-1])
    retrieval._snapshot_retriever.cache_clear()
    try:
        first = retrieval.get_retriever()
        assert retrieval.get_retriever() is first
        version[0] = "two"
        assert retrieval.get_retriever() is not first
        assert len(builds) == 2
    finally:
        retrieval._snapshot_retriever.cache_clear()


def test_runner_rebuilds_agent_on_data_refresh(monkeypatch):
    version = ["one"]
    monkeypatch.setattr(web, "snapshot_signature", lambda: version[0])
    monkeypatch.setattr(graph, "Agent", lambda: object())
    runner = web.Runner()
    first = runner.agent
    assert runner.agent is first
    version[0] = "two"
    assert runner.agent is not first


def test_paused_run_resumes_with_its_own_agent(monkeypatch):
    resumed = []
    old = SimpleNamespace(resume=lambda run_id, answers: resumed.append(run_id) or iter([]))
    runner = web.Runner(lambda: SimpleNamespace())
    monkeypatch.setattr(runner, "_consume", lambda *a: None)
    run = web.Run("paused", {}, {}, "q", agent=old)
    runner.answer(None, run, {"Period?": "2026"})
    assert resumed == ["paused"]


def test_snapshot_change_stops_before_query_execution(monkeypatch):
    agent = object.__new__(graph.Agent)
    agent.snapshot = "old"
    monkeypatch.setattr(graph, "snapshot_signature", lambda: "new")
    result = agent.run_query({"sql": "SELECT 1"})  # No connection: executing SQL would fail this test.
    assert result["status"] == "failed" and result["route"] == "finish"
