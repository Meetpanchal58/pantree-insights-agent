"""Agent paths keep validation and scoped memory while changing only the catalog prompt."""
import sys
from pathlib import Path

import pytest

from pantree_insights import retrieval
from pantree_insights.config import get_settings
from pantree_insights.knowledge import Record
from pantree_insights.retrieval import Retrieved

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
from retrieval_eval import grade, required  # noqa: E402
from test_agent import BY_CATEGORY, Script, make_agent, run, steps  # noqa: E402


def test_retrieval_grading_counts_missing_evidence_instead_of_crediting_fallback():
    item = {"required_tables": ["sales"], "required_metrics": ["orders"]}
    expected = required(item)
    result = Retrieved(["metric:orders"], [Record("table:sales", "table", "Sales", "", "")],
                       .02, .01, True, "hybrid")
    score = grade(result, expected)
    assert score["required_recall"] == .5 and not score["complete_evidence"]
    assert score["missing"] == ["metric:orders"] and score["overflow"]


@pytest.mark.data
def test_default_mode_never_opens_model_or_index(con, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "retrieval_mode", "full")

    def forbidden(*a, **k):
        pytest.fail("Default mode must not load retrieval")
    monkeypatch.setattr(retrieval, "KnowledgeSearch", forbidden)
    agent, _ = make_agent(con, tmp_path, Script(interpreter=[BY_CATEGORY]))
    _, events = run(agent, "revenue by category", use_defaults=True)
    assert events[-1]["status"] == "done" and "retrieve" not in steps(events)


@pytest.mark.data
@pytest.mark.parametrize("mode", ["shadow", "hybrid"])
def test_opt_in_uses_retrieval_on_first_turn_and_correction_without_bypassing_validation(
        con, tmp_path, monkeypatch, mode):
    from pantree_insights.graph import CONFIRM_NO, CONFIRM_QUESTION

    contexts, prompts = [], []
    monkeypatch.setattr(get_settings(), "retrieval_mode", mode)

    class Search:
        startup_seconds, fallback = 0, None

        def __init__(self, settings):
            pass

        def context(self, question, history, answers):
            contexts.append((question, history, answers))
            return ("UNIQUE_RETRIEVED_DEFINITION" if mode == "hybrid" else None), {"mode": mode}

    script = Script(interpreter=[BY_CATEGORY])

    def capture(agent, schema, system, user):
        prompts.append((agent, system))
        return script(agent, schema, system, user)

    monkeypatch.setattr(retrieval, "KnowledgeSearch", Search)
    agent, _ = make_agent(con, tmp_path, capture)
    run_id, events = run(agent, "Revenue by category", confirm=False, history="Earlier brand discussion")
    assert events[-1]["kind"] == "confirm" and "retrieve" in steps(events)
    # A corrected scope must retrieve again, with the settled answers rather than stale first-turn context.
    events = list(agent.resume(run_id, {CONFIRM_QUESTION: CONFIRM_NO, "Correction": "all dates"}))
    assert len(contexts) >= 2 and contexts[-1][1] == "Earlier brand discussion"
    assert contexts[-1][2]  # contains the settled readback correction
    for role, system in prompts:
        if role in ("interpreter", "contrast"):
            assert ("UNIQUE_RETRIEVED_DEFINITION" in system) == (mode == "hybrid")
    # A rollback must ignore persisted hybrid prompt text in an older checkpoint.
    agent.knowledge = None
    prompts.clear()
    events = list(agent.resume(run_id, {CONFIRM_QUESTION: CONFIRM_NO, "Correction": "all categories"}))
    assert prompts and all("UNIQUE_RETRIEVED_DEFINITION" not in system for _, system in prompts)
    assert agent.retrieve({"question": "orders"})["catalog_context"] is None


@pytest.mark.data
def test_real_retrieval_can_feed_agent_and_preserve_compiled_verification(con, tmp_path, monkeypatch):
    if not retrieval.DEFAULT_INDEX.exists():
        pytest.skip("build the local index for this integration smoke test")
    monkeypatch.setattr(get_settings(), "retrieval_mode", "hybrid")
    agent, _ = make_agent(con, tmp_path, Script(interpreter=[BY_CATEGORY]))
    _, events = run(agent, "Realized revenue by category", use_defaults=True)
    assert events[-1]["status"] == "done" and events[-1]["trust"] == "verified"
    trace = next(e["detail"] for e in events if e.get("step") == "retrieve" and e["status"] == "done")
    assert trace["used_context"] == "hybrid" and "metric:realized_revenue" in trace["record_ids"]
