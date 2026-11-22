"""The agent end to end on the small shop with a scripted model: every route, no network, no cost."""
import pytest

from pantree_insights.graph import CONFIRM_QUESTION, CONFIRM_YES
from pantree_insights.llm import FakeLLM
from pantree_insights.memory import Memory

pytestmark = pytest.mark.data

BY_CATEGORY = {"route": "spec", "restated_question": "Realized revenue by product category, all data",
               "spec": {"table": "sales", "measures": [{"name": "realized_revenue", "metric": "realized_revenue"}],
                        "dimensions": ["category"]}}
BY_BRAND = {"table": "sales", "measures": [{"name": "realized_revenue", "metric": "realized_revenue"}],
            "dimensions": ["brand"]}
SAME_NUMBERS = {"table": "sales", "measures": [{"name": "revenue", "metric": "realized_revenue"}],
                "dimensions": ["category"]}
NO_ALTERNATIVES = {"readings": []}


class Script:
    """Answers per agent; each agent pops from its own list (the last answer repeats)."""

    def __init__(self, **answers):
        self.answers = {"contrast": [NO_ALTERNATIVES], "analyst": [analysis_from], **answers}

    def __call__(self, agent, schema, system, user):
        queue = self.answers[agent]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        return item(user) if callable(item) else item


def analysis_from(user: str, wrong: bool = False) -> dict:
    """One claim about the first numeric value of the first row (or none for an empty result)."""
    table = user.split("## Result table\n", 1)[1].split("\n\n", 1)[0].splitlines()
    if len(table) < 2:
        return {"headline": "No rows matched.", "findings": []}
    header, first = table[0].split(","), table[1].split(",")

    def number(text):
        try:
            return float(text)
        except ValueError:
            return None
    col = next(i for i, v in enumerate(first) if number(v) is not None and i > 0)
    claim = {"id": "c1", "value": 123456789.0 if wrong else number(first[col]),
             "unit": "inr" if header[col] in ("realized_revenue", "net_sales", "spend", "revenue") else "number",
             "column": header[col], "row": [{"column": header[0], "value": first[0]}], "op": "value"}
    return {"headline": f"{first[0]} leads with {{c1}}.", "findings": [f"{first[0]} brings {{c1}}."],
            "memory": [{"statement": f"{first[0]} is the top group", "type": "fact"}],
            "chart_metric": header[col], "claims": [claim]}


def make_agent(con, tmp_path, script):
    from pantree_insights.config import ROOT
    from pantree_insights.db import manifest
    from pantree_insights.graph import Agent

    fakes = []

    def factory():
        fakes.append(FakeLLM(script))
        return fakes[-1]

    agent = Agent(llm_factory=factory, memory=Memory(tmp_path / "memory", data_size="small"), con=con,
                  data=manifest(ROOT / "data" / "small"))
    return agent, fakes


def run(agent, question, confirm=True, **kw):
    run_id, events = agent.start(question, **kw)
    events = list(events)
    while confirm and events[-1].get("type") == "clarify" and events[-1].get("kind") == "confirm":
        events += list(agent.resume(run_id, {CONFIRM_QUESTION: CONFIRM_YES}))
    return run_id, events


def steps(events):
    return [e["step"] for e in events if e.get("type") == "step" and e["status"] != "started"]


def test_a_clear_question_is_compiled_verified_and_explained(con, tmp_path):
    agent, fakes = make_agent(con, tmp_path, Script(interpreter=[BY_CATEGORY]))
    _, events = run(agent, "What is realized revenue by category?", use_defaults=True)
    out = events[-1]
    assert out["status"] == "done" and out["trust"] == "verified" and out["grounded"]
    assert "product_master.category AS category" in out["sql"]
    assert steps(events)[:4] == ["check_input", "interpret", "execute", "analyse"]
    assert not fakes[0].prompts  # reviewed grammar and deterministic summary need no model calls
    assert next(i for i, e in enumerate(events) if e["type"] == "result") < next(
        i for i, e in enumerate(events) if e.get("step") == "analyse")


def test_nothing_runs_until_the_readback_is_confirmed(con, tmp_path):
    agent, _ = make_agent(con, tmp_path, Script(interpreter=[BY_CATEGORY]))
    run_id, events = run(agent, "Break down realized revenue by category", confirm=False)
    ask = events[-1]
    assert ask["type"] == "clarify" and ask["kind"] == "confirm" and "Realized revenue" in ask["readback"]
    assert ask["spec"]["dimensions"] == ["category"] and "execute" not in steps(events)
    done = list(agent.resume(run_id, {CONFIRM_QUESTION: CONFIRM_YES}))
    assert done[-1]["status"] == "done"


def test_a_correction_goes_back_to_the_interpreter(con, tmp_path):
    agent, fakes = make_agent(con, tmp_path, Script(interpreter=[BY_CATEGORY, {**BY_CATEGORY, "spec": BY_BRAND}]))
    run_id, events = run(agent, "Revenue split?", confirm=False)
    events = list(agent.resume(run_id, {CONFIRM_QUESTION: "No, by brand please"}))
    assert events[-1]["kind"] == "confirm" and events[-1]["spec"]["dimensions"] == ["brand"]
    assert "by brand please" in [u for a, _, u in fakes[0].prompts if a == "interpreter"][-1]


def test_a_known_two_meaning_phrase_is_always_asked(con, tmp_path):
    agent, fakes = make_agent(con, tmp_path, Script(interpreter=[BY_CATEGORY]))
    _, events = run(agent, "Do customers who get the wrong size return more?", use_defaults=True)
    ask = events[-1]
    assert ask["type"] == "clarify" and "wrong size" in ask["questions"][0]["question"]
    assert not fakes or not fakes[0].prompts                     # asked before any model call


def test_readings_with_different_numbers_are_offered_even_with_defaults(con, tmp_path):
    agent, _ = make_agent(con, tmp_path, Script(interpreter=[BY_CATEGORY],
                                                contrast=[{"readings": [BY_BRAND], "why": ["split"]}]))
    run_id, events = run(agent, "Revenue split?", use_defaults=True)
    ask = events[-1]
    assert ask["type"] == "clarify" and len(ask["questions"][0]["options"]) == 2
    done = list(agent.resume(run_id, {ask["questions"][0]["question"]: ask["questions"][0]["options"][1]}))
    assert done[-1]["status"] == "done" and "brand_name" in done[-1]["sql"]


def test_readings_that_give_the_same_numbers_are_not_asked(con, tmp_path):
    agent, _ = make_agent(con, tmp_path, Script(interpreter=[BY_CATEGORY],
                                                contrast=[{"readings": [SAME_NUMBERS], "why": ["name"]}]))
    _, events = run(agent, "Revenue by category?", use_defaults=True)
    assert events[-1]["status"] == "done"


def test_an_invalid_spec_gets_one_repair_with_the_exact_problem(con, tmp_path):
    bad = {**BY_CATEGORY, "spec": {**BY_CATEGORY["spec"], "filters": [{"field": "city", "values": ["Atlantis"]}]}}
    agent, fakes = make_agent(con, tmp_path, Script(interpreter=[bad, BY_CATEGORY]))
    _, events = run(agent, "Revenue by category in Atlantis?", use_defaults=True)
    repair = [u for a, _, u in fakes[0].prompts if a == "interpreter"][1]
    assert "Atlantis" in repair and "could not be used" in repair and events[-1]["status"] == "done"


def test_a_spec_that_stays_invalid_is_asked_not_guessed(con, tmp_path):
    bad = {**BY_CATEGORY, "spec": {**BY_CATEGORY["spec"], "filters": [{"field": "city", "values": ["Atlantis"]}]}}
    agent, _ = make_agent(con, tmp_path, Script(interpreter=[bad]))
    _, events = run(agent, "Revenue in Atlantis?", use_defaults=True)
    assert events[-1]["type"] == "clarify" and "Atlantis" in events[-1]["questions"][0]["question"]


def test_values_are_normalised_to_their_stored_spelling(con, tmp_path):
    cod = {**BY_CATEGORY, "spec": {**BY_CATEGORY["spec"],
                                   "filters": [{"field": "payment_method", "values": ["cash on delivery"]}]}}
    agent, _ = make_agent(con, tmp_path, Script(interpreter=[cod]))
    _, events = run(agent, "Revenue by category for cash on delivery orders?", use_defaults=True)
    assert "sales.payment_method = 'COD'" in events[-1]["sql"]


def test_a_multi_step_question_uses_a_verified_template(con, tmp_path):
    lift = {"route": "template", "restated_question": "Event lift", "template": {"name": "event_lift",
                                                                               "params": {"days_before": 14}}}
    agent, _ = make_agent(con, tmp_path, Script(interpreter=[lift]))
    _, events = run(agent, "How much did each event lift sales vs the two weeks before?", use_defaults=True)
    assert events[-1]["status"] == "done" and events[-1]["trust"] == "verified"
    assert "INTERVAL 14 DAY" in events[-1]["sql"]


def test_an_unanswerable_question_says_so_instead_of_guessing(con, tmp_path):
    agent, _ = make_agent(con, tmp_path, Script(interpreter=[{
        "route": "unanswerable", "restated_question": "Why customers left",
        "unanswerable_reason": "The data records orders, not reasons for leaving; I can show who stopped ordering."}]))
    _, events = run(agent, "Why did customers leave?", use_defaults=True)
    assert events[-1]["status"] == "declined" and "verified number" in events[-1]["message"]
    assert events[-1]["sql"] is None


def test_a_decline_stops_before_any_query(con, tmp_path):
    agent, _ = make_agent(con, tmp_path, Script(interpreter=[{
        "decline_reason": "Contact details are not available.", "decline_category": "personal_data",
        "restated_question": "phone numbers"}]))
    _, events = run(agent, "Give me the phone list for loyal buyers", use_defaults=True)
    assert events[-1]["status"] == "declined" and events[-1]["sql"] is None


def test_a_period_without_data_is_stated(con, tmp_path):
    december = {**BY_CATEGORY, "spec": {**BY_CATEGORY["spec"], "time": {"start": "2024-12-01", "end": "2024-12-31"}}}
    agent, _ = make_agent(con, tmp_path, Script(interpreter=[december]))
    _, events = run(agent, "Revenue by category in December 2024?", use_defaults=True)
    assert any("no data" in w for w in events[-1]["warnings"])


def test_an_unverifiable_claim_is_removed_not_failing_the_answer(con, tmp_path):
    agent, _ = make_agent(con, tmp_path, Script(interpreter=[BY_CATEGORY],
                                                analyst=[lambda u: analysis_from(u, wrong=True)]))
    _, events = run(agent, "Break down realized revenue by category", use_defaults=True)
    out = events[-1]
    assert out["status"] == "done" and out["grounded"] and "could be checked" in out["analysis"]["headline"]


def test_a_repeat_question_is_served_from_the_cache(con, tmp_path):
    agent, _ = make_agent(con, tmp_path, Script(interpreter=[BY_CATEGORY]))
    run(agent, "Revenue by category?", use_defaults=True)
    sql = next(iter(agent._cache)).split("|", 1)[1]
    calls = []
    agent.con = type("NoDB", (), {"cursor": lambda self: calls.append(1)})()       # the cache must answer
    frame, _ = agent.query(sql)
    assert not frame.empty and not calls


def test_reading_options_show_only_what_differs():
    from pantree_insights.interpret import reading_options
    from pantree_insights.spec import QuerySpec, default_catalog
    a = QuerySpec.model_validate(BY_CATEGORY["spec"])
    b = QuerySpec.model_validate(BY_BRAND)
    options = reading_options([a, b], default_catalog())
    assert options == ["Split by: product category", "Split by: brand"]
