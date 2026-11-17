"""Offline fixtures are inspectable SQL snapshots, never a claim about live-model accuracy."""
import hashlib
import json

import duckdb
import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from pantree_insights import web
from pantree_insights.config import ROOT
from pantree_insights.guardrails import check_sql

FOLDER = ROOT / "demo_runs"
DEMO_RUNS = {p.stem: json.loads(p.read_text()) for p in FOLDER.glob("*.json")}


def test_four_demo_assets_match_the_browser_bundle_and_have_explicit_provenance():
    bundle = (ROOT / "src/pantree_insights/static/offline-demos.js").read_text()
    browser = json.loads(bundle.split("window.PANTREE_DEMOS = ", 1)[1].rstrip(";\n"))
    assert len(browser) == len(DEMO_RUNS) == 4
    assert {d["slug"]: d for d in browser} == DEMO_RUNS
    for demo in browser:
        final = demo["events"][-1]
        assert demo["offline"] and final["offline"] and final["status"] == "done"
        assert demo["question"] == final["question"]
        assert demo["data_as_of"] == demo["provenance"]["manifest"]["last_day"]
        assert hashlib.sha256(final["sql"].encode()).hexdigest() == demo["provenance"]["sql_sha256"]
        assert check_sql(final["sql"]).ok
        assert final["analysis"]["findings"] and final["analysis"]["recommendations"] and final["analysis"]["caveats"]
        assert final["chart"]["kind"] == "offline_bar"
        assert "cost" not in final and "verification" not in final


@pytest.mark.data
def test_saved_sql_results_match_the_pinned_sample_snapshot(con, shop_dir):
    """Only compare the immutable fixture date, not a newer nightly simulator snapshot."""
    live = json.loads((shop_dir / "manifest.json").read_text())
    for demo in DEMO_RUNS.values():
        if demo["provenance"]["manifest"] != live:
            pytest.skip("fixture snapshot differs from the current nightly dataset")
        final = demo["events"][-1]
        actual = con.execute(final["sql"]).df()
        expected = final["result"]
        assert list(actual.columns) == expected["columns"]
        # JSON round-trip uses the same date representation and numeric precision as fixture generation.
        rows = json.loads(actual.to_json(orient="split", index=False, date_format="iso"))["data"]
        assert len(rows) == len(expected["data"])
        for got, want in zip(rows, expected["data"], strict=True):
            for value, saved in zip(got, want, strict=True):
                if isinstance(saved, (int, float)):
                    assert value == pytest.approx(saved, rel=1e-8, abs=1e-6)
                else:
                    assert value == saved


def test_funnel_counts_a_sequence_not_independent_actions():
    sql = DEMO_RUNS["conversion-funnel"]["events"][-1]["sql"]
    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE clickstream(session_id VARCHAR, event_type VARCHAR, event_at TIMESTAMP)")
        con.execute("""INSERT INTO clickstream VALUES
            ('good', 'pdp_view', '2026-01-02 10:00:00'),
            ('good', 'add_to_cart', '2026-01-02 10:01:00'),
            ('good', 'checkout_start', '2026-01-02 10:02:00'),
            ('good', 'purchase', '2026-01-02 10:03:00'),
            ('wrong-order', 'purchase', '2026-01-02 10:00:00'),
            ('wrong-order', 'checkout_start', '2026-01-02 10:01:00'),
            ('wrong-order', 'pdp_view', '2026-01-02 10:02:00'),
            ('wrong-order', 'add_to_cart', '2026-01-02 10:03:00'),
            ('other-session', 'purchase', '2026-01-02 11:00:00')""")
        counts = con.execute(sql).df()["sessions"].tolist()
        assert counts == [3, 2, 2, 1, 1]
    finally:
        con.close()


def test_api_replay_uses_no_model_and_busy_responses_have_a_log_reference(monkeypatch, caplog):
    app = web.create_app()
    monkeypatch.setattr(web.demo_runs, "load", lambda name: DEMO_RUNS[name])

    def forbidden(*args, **kwargs):
        raise AssertionError("An offline demo must not initialize a live agent")

    monkeypatch.setattr(web.Runner, "agent", property(forbidden))

    @app.get("/busy-test")
    def busy():
        return JSONResponse({"detail": "busy"}, status_code=429)

    client = TestClient(app)
    for slug in DEMO_RUNS:
        response = client.get(f"/api/demos/{slug}/stream?instant=true")
        assert response.status_code == 200 and '"offline": true' in response.text
    response = client.get("/busy-test")
    reference = response.headers["X-Request-ID"]
    assert len(reference) == 16 and reference in caplog.text
