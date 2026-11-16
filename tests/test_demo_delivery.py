"""Recorded demos can load immediately while animated replay remains available."""
import json

from fastapi.testclient import TestClient

from pantree_insights import web


def test_instant_demo_skips_delays(monkeypatch):
    demo = {"events": [
        {"type": "step", "step": "plan", "status": "started", "t": 0},
        {"type": "step", "step": "plan", "status": "done", "t": 10},
        {"type": "final", "status": "done", "t": 20},
    ]}
    monkeypatch.setattr(web.demo_runs, "load", lambda name: demo)
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(web.asyncio, "sleep", sleep)
    client = TestClient(web.create_app())  # No startup/data/model initialization needed.
    response = client.get("/api/demos/sample/stream?instant=true")
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert [e["type"] for e in events] == ["step", "step", "final"]
    assert all(e["replay"] for e in events)
    assert delays == []
    client.get("/api/demos/sample/stream")
    assert delays == [3.0, 3.0]
