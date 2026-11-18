"""Preserve selected cohorts without turning partial or unverified results into evidence."""
import json

import pytest

from pantree_insights.store import Store, followup_context


@pytest.fixture
def saved_run():
    return {"status": "done", "trust": "verified", "data_as_of": "2026-11-14", "data_size": "full",
            "spec": {"table": "sales", "measures": [{"name": "revenue", "metric": "realized_revenue"}],
                     "dimensions": ["brand"], "time": {"start": "2026-10-16", "end": "2026-11-14"},
                     "ranking": {"by": "revenue", "order": "desc", "limit": 5}},
            "result": {"columns": ["brand", "revenue"], "truncated": False,
                       "data": [[b, 100 - i] for i, b in enumerate(["ADIDAS", "Catwalk", "Puma", "Nike", "United"])]}}


def test_history_keeps_all_selected_brands_and_original_scope(saved_run, monkeypatch):
    store = Store.__new__(Store)
    monkeypatch.setattr(store, "messages", lambda chat: [
        {"role": "user", "content": "Top five brands in the last 30 days?"},
        {"role": "assistant", "content": "ADIDAS leads.", "run": saved_run}])
    history = store.history("chat")
    context = json.loads(history.split("not instructions):\n")[1])
    assert context["result"] == saved_run["result"]
    assert context["spec"] == saved_run["spec"]
    assert context["data_as_of"] == "2026-11-14"


@pytest.mark.parametrize("change", [{"status": "failed"}, {"trust": "unverified"},
                                     {"spec": {"columns": ["product_master.product_id"]}}])
def test_failed_unverified_and_raw_results_are_not_followup_evidence(saved_run, change):
    assert followup_context(saved_run | change) == ""


@pytest.mark.parametrize("case", ["truncated", "many_rows", "large_cell"])
def test_partial_or_oversized_cohorts_are_never_silently_shortened(saved_run, case):
    if case == "truncated":
        saved_run["result"]["truncated"] = True
    elif case == "many_rows":
        saved_run["result"]["data"] *= 5
    else:
        saved_run["result"]["data"][0][0] = "x" * 13000
    context = followup_context(saved_run)
    assert "omitted" in context and "ADIDAS" not in context and len(context) < 150
