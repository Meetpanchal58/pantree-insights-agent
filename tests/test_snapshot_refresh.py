"""Rebuild metadata on a published manifest change, and don't run SQL with a stale interpretation."""
from types import SimpleNamespace

from pantree_insights import db, graph, web


def test_manifest_signature_includes_version_and_location(tmp_path, monkeypatch):
    prompts = tmp_path / "prompt.txt"
    prompts.write_text("instructions")
    monkeypatch.setattr(db, "get_settings", lambda: SimpleNamespace(
        shop_dir=tmp_path, semantic_dir=tmp_path, prompts_file=prompts))
    path = tmp_path / "manifest.json"
    path.write_text('{"last_day":"2026-11-15"}')
    old = db.snapshot_signature()
    path.write_text('{"last_day":"2026-11-16"}')
    assert db.snapshot_signature() != old
    old = db.snapshot_signature()
    (tmp_path / "metrics.yaml").write_text("changed definition")
    assert db.snapshot_signature() != old
    old = db.snapshot_signature()
    prompts.write_text("changed instructions")
    assert db.snapshot_signature() != old


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
    old = SimpleNamespace(resume=lambda run_id, message, version: resumed.append(run_id) or iter([]))
    runner = web.Runner(lambda: SimpleNamespace())
    monkeypatch.setattr(runner, "_consume", lambda *a: None)
    run = web.Run("paused", {}, {}, "q", agent=old)
    run.paused = True
    run.events = [{"type": "clarify", "plan_version": 0}]
    runner.answer(None, run, "2026", 0)
    assert resumed == ["paused"]


def test_snapshot_change_stops_before_saving_an_insight(monkeypatch):
    agent = object.__new__(graph.Agent)
    agent.snapshot = "old"
    monkeypatch.setattr(graph, "snapshot_signature", lambda: "new")
    result = agent.remember({"run_id": "r", "analysis": {}, "question": "q"})
    assert result["status"] == "failed" and result["insight_id"] is None
