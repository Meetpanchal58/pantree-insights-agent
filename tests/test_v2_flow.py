"""v2 end to end on the small shop with a scripted model: every route of interpret -> confirm -> execute."""
# ruff: noqa: F811  (pytest fixtures are imported from test_graph and requested by name)
import pytest
from test_graph import Script, analysis_from, make_agent, resume, retriever, run, steps  # noqa: F401

from pantree_insights.graph import CONFIRM_QUESTION, CONFIRM_YES, VARIANTS

pytestmark = pytest.mark.data

BY_CATEGORY = {"route": "spec", "restated_question": "Realized revenue by product category, all data",
               "spec": {"table": "sales", "measures": [{"name": "realized_revenue", "metric": "realized_revenue"}],
                        "dimensions": ["category"]}}
BY_BRAND = {**BY_CATEGORY, "spec": {**BY_CATEGORY["spec"], "dimensions": ["brand"]}}
BY_CITY = {**BY_CATEGORY, "spec": {**BY_CATEGORY["spec"], "dimensions": ["city"]}}


def v2(con, retriever, tmp_path, **answers):
    return make_agent(con, retriever, tmp_path, Script(analyst=[analysis_from], **answers), features=VARIANTS["v2"])


def final(events):
    return events[-1]


def test_a_clear_question_is_compiled_verified_and_answered(con, retriever, tmp_path):
    agent, fakes, _ = v2(con, retriever, tmp_path, interpreter=[BY_CATEGORY])
    _, events = run(agent, "What is realized revenue by category?", use_defaults=True)
    out = final(events)
    assert out["status"] == "done" and out["trust"] == "verified"
    assert "product_master.category AS category" in out["sql"]
    assert out["verification"]["status"] == "verified" and out["grounded"]
    assert [s for s, st in steps(events) if st != "started"][:4] == ["check_input", "interpret", "execute", "analyse"]
    agents_called = [a for a, _, _ in fakes[0].prompts]
    assert agents_called.count("interpreter") == 2 and "sql_writer" not in agents_called   # agreement, no SQL model


def test_the_readback_is_confirmed_before_anything_runs(con, retriever, tmp_path):
    agent, _, _ = v2(con, retriever, tmp_path, interpreter=[BY_CATEGORY])
    run_id, events = run(agent, "What is realized revenue by category?", confirm=False)
    ask = final(events)
    assert ask["type"] == "clarify" and ask["kind"] == "confirm"
    assert "Realized revenue" in ask["readback"] and "category" in ask["readback"]
    assert "execute" not in [s for s, _ in steps(events)]
    done = resume(agent, run_id, {CONFIRM_QUESTION: CONFIRM_YES})
    assert final(done)["status"] == "done" and final(done)["trust"] == "verified"


def test_a_third_reading_breaks_a_tie(con, retriever, tmp_path):
    agent, _, _ = v2(con, retriever, tmp_path, interpreter=[BY_CATEGORY, BY_BRAND, BY_CATEGORY])
    _, events = run(agent, "Revenue by category?", use_defaults=True)
    assert final(events)["trust"] == "verified" and "category" in final(events)["sql"]


def test_three_different_readings_are_asked_even_with_defaults(con, retriever, tmp_path):
    agent, _, _ = v2(con, retriever, tmp_path, interpreter=[BY_CATEGORY, BY_BRAND, BY_CITY])
    run_id, events = run(agent, "Revenue split?", use_defaults=True)
    ask = final(events)
    assert ask["type"] == "clarify" and len(ask["questions"][0]["options"]) == 4     # 3 readings + none
    chosen = ask["questions"][0]["options"][1]
    done = resume(agent, run_id, {ask["questions"][0]["question"]: chosen})
    assert final(done)["status"] == "done" and "brand_name" in final(done)["sql"]


def test_an_invalid_spec_gets_one_repair_with_the_exact_problem(con, retriever, tmp_path):
    bad = {**BY_CATEGORY, "spec": {**BY_CATEGORY["spec"], "filters": [{"field": "city", "values": ["Atlantis"]}]}}
    agent, fakes, _ = v2(con, retriever, tmp_path, interpreter=[bad, BY_CATEGORY])
    _, events = run(agent, "Revenue by category in Atlantis?", use_defaults=True)
    repair = [u for a, _, u in fakes[0].prompts if a == "interpreter"][1]
    assert "Atlantis" in repair and "could not be used" in repair
    assert final(events)["trust"] == "verified"


def test_a_spec_that_stays_invalid_is_asked_not_guessed(con, retriever, tmp_path):
    bad = {**BY_CATEGORY, "spec": {**BY_CATEGORY["spec"], "filters": [{"field": "city", "values": ["Atlantis"]}]}}
    agent, _, _ = v2(con, retriever, tmp_path, interpreter=[bad])
    _, events = run(agent, "Revenue in Atlantis?", use_defaults=True)
    assert final(events)["type"] == "clarify" and "Atlantis" in final(events)["questions"][0]["question"]


def test_values_are_normalised_to_their_stored_spelling(con, retriever, tmp_path):
    cod = {**BY_CATEGORY, "spec": {**BY_CATEGORY["spec"],
                                   "filters": [{"field": "payment_method", "values": ["cash on delivery"]}]}}
    agent, _, _ = v2(con, retriever, tmp_path, interpreter=[cod])
    _, events = run(agent, "Revenue by category for cash on delivery orders?", use_defaults=True)
    assert "sales.payment_method = 'COD'" in final(events)["sql"]


def test_a_decline_stops_before_any_query(con, retriever, tmp_path):
    agent, _, _ = v2(con, retriever, tmp_path, interpreter=[
        {"decline_reason": "Contact details of customers are not available.", "decline_category": "personal_data",
         "restated_question": "customer phone numbers"}])
    _, events = run(agent, "Give me the phone list for loyal buyers", use_defaults=True)
    assert final(events)["status"] == "declined" and final(events)["sql"] is None


def test_a_multi_step_question_goes_to_the_exploratory_pipeline_labelled(con, retriever, tmp_path):
    from test_graph import GOOD_SQL, PLAN
    agent, _, _ = v2(con, retriever, tmp_path,
                     interpreter=[{"route": "analysis", "restated_question": "event lift vs the 14 days before"}],
                     planner=[PLAN], sql_writer=[{"sql": GOOD_SQL, "explanation": "x"}], validator=[{"ok": True}])
    _, events = run(agent, "How much did each event lift sales vs the two weeks before?", use_defaults=True)
    assert final(events)["trust"] == "exploratory"
    assert "write_sql" in [s for s, _ in steps(events)]


def test_an_unverifiable_claim_is_dropped_not_failing_the_answer(con, retriever, tmp_path):
    agent, _, _ = v2(con, retriever, tmp_path, interpreter=[BY_CATEGORY])
    agent.llm_factory = lambda: __import__("pantree_insights.llm", fromlist=["FakeLLM"]).FakeLLM(
        Script(interpreter=[BY_CATEGORY], analyst=[lambda u: analysis_from(u, wrong=True)]))
    _, events = run(agent, "What is realized revenue by category?", use_defaults=True)
    out = final(events)
    assert out["status"] == "done" and out["trust"] == "verified"
    assert out["grounded"] and "could be checked" in out["analysis"]["headline"]
