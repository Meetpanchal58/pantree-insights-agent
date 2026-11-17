"""The agent flow end to end on the small shop, with a scripted fake LLM (no network, no cost)."""
import json
import re

import pytest

from pantree_insights.llm import FakeLLM
from pantree_insights.memory import Memory

pytestmark = pytest.mark.data

GOOD_SQL = """SELECT p.category,
  SUM(CASE WHEN s.delivered_at IS NOT NULL THEN s.net_amount - COALESCE(s.refund_amount, 0) ELSE 0 END)
    AS realized_revenue,
  COUNT(DISTINCT s.order_id) AS orders
FROM sales s JOIN product_master p ON p.product_id = s.product_id
WHERE s.line_type = 'Sale' GROUP BY 1 ORDER BY realized_revenue DESC"""

PLAN = {"decision": "proceed", "restated_question": "Realized revenue by category, all time",
        "metrics": ["realized_revenue"], "breakdowns": ["category"], "time_window": "all data",
        "output_shape": "one row per category"}


CLEAR_FRAME = {"restated_question": "Realized revenue by category, all data", "subjects": ["orders"],
               "measures": ["realized_revenue"], "breakdowns": ["category"], "open_points": []}


def analysis_from(user_prompt: str, wrong: bool = False) -> dict:
    """An analyst answer with one claim about the first result row (or a made-up value)."""
    table = user_prompt.split("## Result table\n", 1)[1].split("\n\n", 1)[0].splitlines()
    header, first_row = table[0].split(","), table[1].split(",")
    value = 123456789.0 if wrong else float(first_row[1])
    claim = {"id": "c1", "value": value, "unit": "inr", "column": header[1],
             "row": [{"column": header[0], "value": first_row[0]}], "op": "value"}
    return {"headline": f"{first_row[0]} leads with Rs {{c1}} realized revenue.",
            "findings": [f"{first_row[0]} brings Rs {{c1}}."], "recommendations": ["Keep investing."],
            "memory": [{"statement": f"{first_row[0]} is the top category by realized revenue", "type": "fact"}],
            "chart_title": "Realized revenue by category", "chart_metric": "realized_revenue", "claims": [claim]}


class Script:
    """Scripted answers per agent; each agent pops from its own list (the last answer repeats)."""

    def __init__(self, **answers):
        self.answers = {k: list(v) for k, v in answers.items()}

    def __call__(self, agent, schema, system, user):
        if agent == "understander" and agent not in self.answers:
            return CLEAR_FRAME                       # most tests are about later steps: a clear question
        queue = self.answers[agent]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        return item(user) if callable(item) else item


@pytest.fixture(scope="module")
def retriever(con):
    from pantree_insights.retrieval import Retriever

    return Retriever(con)


def make_agent(con, retriever, tmp_path, script, features=None):
    from pantree_insights.db import manifest
    from pantree_insights.graph import Agent

    fakes = []

    def factory():
        fakes.append(FakeLLM(script))
        return fakes[-1]

    memory = Memory(tmp_path / "memory", data_size="small")
    agent = Agent(llm_factory=factory, retriever=retriever, memory=memory, con=con,
                  data=manifest(retriever_shop(con)), features=features)
    return agent, fakes, memory


def retriever_shop(con):
    from pantree_insights.config import ROOT
    return ROOT / "data" / "small"


def _auto_confirm(agent, run_id, events):
    """Most tests are about other steps: confirm the read-back whenever it is shown."""
    from pantree_insights.graph import CONFIRM_QUESTION, CONFIRM_YES

    while events and events[-1].get("type") == "clarify" and events[-1].get("kind") == "confirm":
        events = events + list(agent.resume(run_id, {CONFIRM_QUESTION: CONFIRM_YES}))
    return events


def run(agent, question, confirm=True, **kw):
    run_id, events = agent.start(question, **kw)
    events = list(events)
    return run_id, _auto_confirm(agent, run_id, events) if confirm else events


def resume(agent, run_id, answers, confirm=True):
    events = list(agent.resume(run_id, answers))
    return _auto_confirm(agent, run_id, events) if confirm else events


def steps(events):
    return [(e["step"], e["status"]) for e in events if e.get("type") == "step"]


def test_happy_path(con, retriever, tmp_path):
    agent, fakes, memory = make_agent(con, retriever, tmp_path, Script(
        planner=[PLAN], sql_writer=[{"sql": GOOD_SQL, "explanation": "revenue by category"}],
        validator=[{"ok": True}], analyst=[analysis_from]))
    _, events = run(agent, "What is revenue by category?")
    final = events[-1]
    assert final["status"] == "done", final
    assert [s for s, st in steps(events) if st == "done"] == [
        "check_input", "retrieve", "plan", "write_sql", "check_sql", "run_query", "validate",
        "analyse", "chart", "remember"]
    assert ("understand", "needs_input") in steps(events)          # paused for the read-back, then confirmed
    assert final["chart"]["kind"] in ("bar", "bar_h")
    assert final["result"]["columns"] == ["category", "realized_revenue", "orders"]
    assert final["warnings"] == []
    assert final["cost"]["calls"] == 5
    saved = memory.insights()
    assert len(saved) == 1 and saved[0]["sql"].startswith("SELECT")
    # the planner saw the retrieved context; the writer saw the plan
    agents_called = [a for a, _, _ in fakes[0].prompts]
    assert agents_called == ["understander", "planner", "sql_writer", "validator", "analyst"]
    assert "## Tables" in fakes[0].prompts[1][2] and "Realized revenue by category" in fakes[0].prompts[2][2]
    # the resolved understanding reaches the planner and the writer, with the code-built join path
    assert "JOIN product_master ON sales.product_id = product_master.product_id" in fakes[0].prompts[2][2]
    json.dumps(final)                                                  # everything is JSON for the web app


def test_guardrail_and_database_errors_are_fed_back(con, retriever, tmp_path):
    agent, fakes, _ = make_agent(con, retriever, tmp_path, Script(
        planner=[PLAN],
        sql_writer=[{"sql": "SELECT * FROM sales", "explanation": "x"},
                    {"sql": "SELECT CAST(payment_method AS INTEGER) AS n, COUNT(*) FROM sales GROUP BY 1",
                     "explanation": "x"},
                    {"sql": GOOD_SQL, "explanation": "ok"}],
        validator=[{"ok": True}], analyst=[analysis_from]))
    _, events = run(agent, "What is revenue by category?")
    assert events[-1]["status"] == "done" and events[-1]["attempts"] == 3
    assert ("check_sql", "retry") in steps(events) and ("run_query", "retry") in steps(events)
    writer_prompts = [u for a, _, u in fakes[0].prompts if a == "sql_writer"]
    assert "SELECT * is not allowed" in writer_prompts[1]
    assert "Database error" in writer_prompts[2]


def test_validator_feedback_then_success(con, retriever, tmp_path):
    agent, fakes, _ = make_agent(con, retriever, tmp_path, Script(
        planner=[PLAN], sql_writer=[{"sql": GOOD_SQL, "explanation": "x"}],
        validator=[{"ok": False, "problems": ["Use realized revenue, not net sales"]}, {"ok": True}],
        analyst=[analysis_from]))
    _, events = run(agent, "What is revenue by category?")
    assert events[-1]["status"] == "done" and ("validate", "retry") in steps(events)
    assert "Use realized revenue" in [u for a, _, u in fakes[0].prompts if a == "sql_writer"][1]


def test_gives_up_after_max_attempts(con, retriever, tmp_path):
    agent, _, memory = make_agent(con, retriever, tmp_path, Script(
        planner=[PLAN], sql_writer=[{"sql": "DROP TABLE sales", "explanation": "x"}],
        validator=[{"ok": True}], analyst=[analysis_from]))
    _, events = run(agent, "What is revenue by category?")
    assert events[-1]["status"] == "failed" and "3 attempts" in events[-1]["message"]
    assert memory.insights() == []


def test_planner_clarifies_when_understanding_is_disabled(con, retriever, tmp_path):
    from pantree_insights.graph import VARIANTS

    clarify = {"decision": "clarify", "restated_question": "Sales by category",
               "clarifying_questions": [{"question": "Which sales measure?", "options": ["Realized revenue", "Orders"],
                                         "default": "Realized revenue", "why": "changes the ranking"}]}

    def planner(user):
        return PLAN if "answers to clarifying questions" in user else clarify

    agent, fakes, _ = make_agent(con, retriever, tmp_path, Script(
        planner=[planner], sql_writer=[{"sql": GOOD_SQL, "explanation": "x"}],
        validator=[{"ok": True}], analyst=[analysis_from]), features=VARIANTS["no_understanding"])
    run_id, events = run(agent, "How are sales by category?")
    assert events[-1]["type"] == "clarify" and events[-1]["questions"][0]["default"] == "Realized revenue"
    resumed = resume(agent, run_id, {"Which sales measure?": "Realized revenue"})
    assert resumed[-1]["status"] == "done"
    planner_prompts = [u for a, _, u in fakes[0].prompts if a == "planner"]
    assert "Which sales measure?: Realized revenue" in planner_prompts[-1]


def test_use_defaults_never_asks(con, retriever, tmp_path):
    clarify = {"decision": "clarify", "restated_question": "x", "metrics": ["realized_revenue"],
               "clarifying_questions": [{"question": "Period?", "options": ["All", "2026"], "default": "All",
                                         "why": "x"}]}
    agent, _, _ = make_agent(con, retriever, tmp_path, Script(
        planner=[clarify], sql_writer=[{"sql": GOOD_SQL, "explanation": "x"}], validator=[{"ok": True}],
        analyst=[analysis_from]))
    _, events = run(agent, "Revenue by category", use_defaults=True)
    assert events[-1]["status"] == "done"
    assert any("Assumed 'All'" in a for a in events[-1]["plan"]["assumptions"])


def test_personal_data_blocked_before_any_llm_call(con, retriever, tmp_path):
    agent, fakes, _ = make_agent(con, retriever, tmp_path, Script(planner=[PLAN]))
    _, events = run(agent, "List the email addresses of customers in Pune")
    assert events[-1]["status"] == "declined"
    assert not fakes or fakes[0].prompts == []                         # no LLM call was made


def test_planner_decline(con, retriever, tmp_path):
    agent, _, _ = make_agent(con, retriever, tmp_path, Script(
        planner=[{"decision": "decline", "restated_question": "x", "decline_reason": "Not about the shop."}]))
    _, events = run(agent, "What will the weather be tomorrow?")
    assert events[-1]["status"] == "declined" and events[-1]["message"] == "Not about the shop."


def test_unverified_claims_trigger_a_rewrite(con, retriever, tmp_path):
    answers = [lambda u: analysis_from(u, wrong=True), analysis_from]
    agent, _, _ = make_agent(con, retriever, tmp_path, Script(
        planner=[PLAN], sql_writer=[{"sql": GOOD_SQL, "explanation": "x"}], validator=[{"ok": True}],
        analyst=answers))
    _, events = run(agent, "What is revenue by category?")
    analyse_done = [e for e in events if e.get("step") == "analyse" and e["status"] == "done"][0]
    assert analyse_done["detail"]["rewritten"] and analyse_done["detail"]["number_check"] == "passed"
    assert events[-1]["warnings"] == []


def test_sql_writer_gets_only_plan_tables_then_full_context_on_retry(con, retriever, tmp_path):
    plan = {**PLAN, "source_tables": ["sales", "product_master"],
            "join_path": ["sales.product_id = product_master.product_id"]}
    agent, fakes, _ = make_agent(con, retriever, tmp_path, Script(
        planner=[plan],
        sql_writer=[{"sql": "SELECT * FROM sales", "explanation": "x"}, {"sql": GOOD_SQL, "explanation": "ok"}],
        validator=[{"ok": True}], analyst=[analysis_from]))
    _, events = run(agent, "Which marketing campaigns drive realized revenue by product category?")
    assert events[-1]["status"] == "done"
    first, retry = [u for a, _, u in fakes[0].prompts if a == "sql_writer"]
    first_tables = set(re.findall(r"TABLE (\w+)", first))
    assert first_tables == {"sales", "product_master"}
    assert len(set(re.findall(r"TABLE (\w+)", retry))) > 2           # retry widens to the full retrieved context
    detail = [e for e in events if e.get("step") == "write_sql" and e["status"] == "done"][0]["detail"]
    assert detail["context_tables"] == ["sales", "product_master"]


def test_ungrounded_answer_is_flagged_after_one_rewrite(con, retriever, tmp_path):
    agent, _, _ = make_agent(con, retriever, tmp_path, Script(
        planner=[PLAN], sql_writer=[{"sql": GOOD_SQL, "explanation": "x"}], validator=[{"ok": True}],
        analyst=[lambda u: analysis_from(u, wrong=True)]))
    _, events = run(agent, "What is revenue by category?")
    final = events[-1]
    assert final["status"] == "failed" and final["grounded"] is False
    assert any(w.startswith("Unverified claims") for w in final["warnings"])
    assert "{c1}" not in final["analysis"]["headline"]                 # placeholders are rendered


def test_ungrounded_or_disputed_answers_are_not_remembered(con, retriever, tmp_path):
    agent, _, memory = make_agent(con, retriever, tmp_path, Script(
        planner=[PLAN], sql_writer=[{"sql": GOOD_SQL, "explanation": "x"}], validator=[{"ok": True}],
        analyst=[lambda u: analysis_from(u, wrong=True)]))
    _, events = run(agent, "What is revenue by category?")
    assert events[-1]["status"] == "failed"
    assert not any(e.get("step") == "remember" for e in events)
    assert memory.insights() == []
    agent, _, memory = make_agent(con, retriever, tmp_path / "b", Script(
        planner=[PLAN], sql_writer=[{"sql": GOOD_SQL, "explanation": "x"}],
        validator=[{"ok": False, "problems": ["Wrong denominator"]}], analyst=[analysis_from]))
    _, events = run(agent, "What is revenue by category?")
    assert events[-1]["status"] == "failed" and memory.insights() == []   # no answer after failed review


def test_empty_result_goes_to_the_validator(con, retriever, tmp_path):
    empty = ("SELECT COUNT(DISTINCT order_id) AS orders FROM sales WHERE placed_at < TIMESTAMP '2000-01-01' "
             "GROUP BY line_type")
    agent, fakes, _ = make_agent(con, retriever, tmp_path, Script(
        planner=[PLAN], sql_writer=[{"sql": empty, "explanation": "x"}], validator=[{"ok": True}],
        analyst=[{"headline": "No orders were placed before the shop opened.", "findings": ["Nothing to report."]}]))
    _, events = run(agent, "How many orders were placed before 2000?")
    assert events[-1]["status"] == "done" and events[-1]["attempts"] == 1
    assert events[-1]["result"]["data"] == []


def test_old_insights_are_left_out_of_memory_context(tmp_path):
    memory = Memory(tmp_path / "m", data_size="small")
    analysis = {"headline": "Footwear returns are high", "memory": []}
    memory.save_insight(question="footwear returns", plan={}, sql="SELECT 1", analysis=analysis,
                        data_as_of="2026-09-01")
    assert "footwear returns" in memory.context("footwear returns", as_of="2026-09-20").lower()
    assert "footwear returns" not in memory.context("footwear returns", as_of="2026-11-14").lower()


@pytest.mark.parametrize("variant,expected", [
    ("baseline", ["sql_writer", "analyst"]),
    ("retrieval", ["sql_writer", "analyst"]),
    ("validator", ["planner", "sql_writer", "validator", "analyst"]),
    ("no_understanding", ["planner", "sql_writer", "validator", "analyst"]),
    ("full", ["understander", "planner", "sql_writer", "validator", "analyst"]),
])
def test_variants_switch_agents(con, retriever, tmp_path, variant, expected):
    from pantree_insights.db import manifest
    from pantree_insights.graph import VARIANTS, Agent

    fakes = []

    def factory():
        fakes.append(FakeLLM(Script(planner=[PLAN], sql_writer=[{"sql": GOOD_SQL, "explanation": "x"}],
                                    validator=[{"ok": True}], analyst=[analysis_from])))
        return fakes[-1]

    agent = Agent(llm_factory=factory, retriever=retriever, memory=Memory(tmp_path / "m", data_size="small"),
                  con=con, data=manifest(retriever_shop(con)), features=VARIANTS[variant])
    _, events = run(agent, "What is revenue by category?")
    assert events[-1]["status"] == "done"
    assert [a for a, _, _ in fakes[0].prompts] == expected
    writer_prompt = [u for a, _, u in fakes[0].prompts if a == "sql_writer"][0]
    if variant == "baseline":
        assert "TABLE support_contacts" in writer_prompt and "Metric definitions" not in writer_prompt


def test_empty_sql_gets_specific_feedback_and_the_writers_reason_is_kept(con, retriever, tmp_path):
    """Seen in the paid runs: the writer returned no SQL three times and the failure said only 'found 0'."""
    agent, fakes, _ = make_agent(con, retriever, tmp_path, Script(
        planner=[PLAN],
        sql_writer=[{"sql": "  ", "explanation": "Region of the delivery address may be personal data"},
                    {"sql": GOOD_SQL, "explanation": "ok"}],
        validator=[{"ok": True}], analyst=[analysis_from]))
    _, events = run(agent, "What is revenue by category?")
    assert events[-1]["status"] == "done" and events[-1]["attempts"] == 2
    retry_prompt = [u for a, _, u in fakes[0].prompts if a == "sql_writer"][1]
    assert "No SQL was returned" in retry_prompt and "may be personal data" in retry_prompt

    agent, _, _ = make_agent(con, retriever, tmp_path, Script(
        planner=[PLAN], sql_writer=[{"sql": "", "explanation": "cannot answer this"}],
        validator=[{"ok": True}], analyst=[analysis_from]))
    _, events = run(agent, "What is revenue by category?")
    assert events[-1]["status"] == "failed" and "cannot answer this" in events[-1]["message"]


# ----------------------------------------------------------------------------- understanding + clarifying loop
def ask(*questions):
    return {**CLEAR_FRAME, "open_points": [{"question": q, "options": ["A", "B"], "default": "A", "why": "w"}
                                           for q in questions]}


def test_keeps_asking_round_after_round_until_clear(con, retriever, tmp_path):
    """No fixed limit: 4 questions in round 1, a follow-up in round 2 opened by an answer, then clear."""
    def understander(user):
        if "Which channel?: B" in user:
            return CLEAR_FRAME
        if "Which metric?: A" in user:
            return ask("Which channel?")
        return ask("Which metric?", "Which period?", "Which baseline?", "What does best mean?")

    agent, fakes, _ = make_agent(con, retriever, tmp_path, Script(
        understander=[understander], planner=[PLAN], sql_writer=[{"sql": GOOD_SQL, "explanation": "x"}],
        validator=[{"ok": True}], analyst=[analysis_from]))
    run_id, events = run(agent, "What is our best channel?")
    assert events[-1]["type"] == "clarify" and len(events[-1]["questions"]) == 4 and events[-1]["round"] == 1
    second = resume(agent, run_id, {"Which metric?": "A", "Which period?": "B", "Which baseline?": "A",
                                        "What does best mean?": "A"})
    assert second[-1]["type"] == "clarify" and [q["question"] for q in second[-1]["questions"]] == ["Which channel?"]
    assert second[-1]["round"] == 2
    final = resume(agent, run_id, {"Which channel?": "B"})[-1]
    assert final["status"] == "done"
    planner_prompt = [u for a, _, u in fakes[0].prompts if a == "planner"][-1]
    assert "Which period?: B" in planner_prompt and "Which channel?: B" in planner_prompt   # answers accumulate


def test_use_defaults_answers_every_open_point_without_pausing(con, retriever, tmp_path):
    agent, fakes, _ = make_agent(con, retriever, tmp_path, Script(
        understander=[ask("Which metric?", "Which period?")], planner=[PLAN],
        sql_writer=[{"sql": GOOD_SQL, "explanation": "x"}], validator=[{"ok": True}], analyst=[analysis_from]))
    _, events = run(agent, "What is our best channel?", use_defaults=True)
    assert events[-1]["status"] == "done" and not any(e["type"] == "clarify" for e in events)
    assert "Which metric?: A" in [u for a, _, u in fakes[0].prompts if a == "planner"][-1]


def test_safety_stop_after_max_rounds_does_not_compute_guessed_values(con, retriever, tmp_path):
    from pantree_insights.graph import MAX_CLARIFY_ROUNDS

    def understander(user):                                  # a confused model: always a new question
        return ask(f"Question {user.count(': A')}?")

    agent, _, _ = make_agent(con, retriever, tmp_path, Script(
        understander=[understander], planner=[PLAN], sql_writer=[{"sql": GOOD_SQL, "explanation": "x"}],
        validator=[{"ok": True}], analyst=[analysis_from]))
    run_id, events = run(agent, "Something vague")
    rounds = 0
    while events[-1]["type"] == "clarify":
        rounds += 1
        events = resume(agent, run_id, {events[-1]["questions"][0]["question"]: "A"})
    assert rounds == MAX_CLARIFY_ROUNDS and events[-1]["status"] == "declined"
    assert not any(e.get("step") == "write_sql" for e in events)


def test_which_mumbai_is_asked_and_the_answer_reaches_the_sql_writer(con, retriever, tmp_path):
    frame = {**CLEAR_FRAME, "restated_question": "Orders in Mumbai", "breakdowns": [], "measures": ["orders"],
             "mentions": [{"text": "Mumbai", "concept": "other"}]}
    agent, fakes, _ = make_agent(con, retriever, tmp_path, Script(
        understander=[frame], planner=[PLAN], sql_writer=[{"sql": WITH_FILTER, "explanation": "x"}],
        validator=[{"ok": True}], analyst=[analysis_from]))
    run_id, events = run(agent, "How many orders in Mumbai?")
    question = events[-1]["questions"][0]
    assert "Mumbai" in question["question"] and len(question["options"]) == 4     # city, seller, warehouse, brand
    seller = next(o for o in question["options"] if "seller" in o)
    assert resume(agent, run_id, {question["question"]: seller})[-1]["status"] == "done"
    writer = [u for a, _, u in fakes[0].prompts if a == "sql_writer"][0]
    assert "Filter: sellers.city = 'Mumbai'" in writer
    assert "JOIN sellers ON sales.seller_id = sellers.seller_id" in writer


def test_order_number_is_resolved_to_its_id(con, retriever, tmp_path):
    agent, fakes, _ = make_agent(con, retriever, tmp_path, Script(
        planner=[PLAN], sql_writer=[{"sql": GOOD_SQL, "explanation": "x"}], validator=[{"ok": True}],
        analyst=[analysis_from]))
    run(agent, "What was in order 12?")
    writer = [u for a, _, u in fakes[0].prompts if a == "sql_writer"][0]
    assert "Filter: order_id = 'OD000000012'" in writer


# ----------------------------------------------------------------------------- step 4: references and result checks
MUMBAI_SELLERS = {**CLEAR_FRAME, "restated_question": "Orders from Mumbai sellers", "breakdowns": [],
                  "measures": ["orders"], "mentions": [{"text": "Mumbai", "concept": "seller_city"}]}
WITH_FILTER = ("SELECT se.city, COUNT(DISTINCT s.order_id) AS orders FROM sales s JOIN sellers se "
               "ON se.seller_id = s.seller_id WHERE s.line_type = 'Sale' AND se.city = 'Mumbai' GROUP BY 1")


def test_sql_that_ignores_a_resolved_filter_is_sent_back(con, retriever, tmp_path):
    agent, fakes, _ = make_agent(con, retriever, tmp_path, Script(
        understander=[MUMBAI_SELLERS], planner=[PLAN],
        sql_writer=[{"sql": GOOD_SQL, "explanation": "forgot the filter"}, {"sql": WITH_FILTER, "explanation": "ok"}],
        validator=[{"ok": True}], analyst=[analysis_from]))
    _, events = run(agent, "How many orders came from sellers in Mumbai?")
    assert events[-1]["status"] == "done" and events[-1]["attempts"] == 2
    retry = [u for a, _, u in fakes[0].prompts if a == "sql_writer"][1]
    assert "resolved to the filter sellers.city = 'Mumbai'" in retry


def test_structural_references_reach_the_writer_and_respect_hidden_examples(con, retriever, tmp_path):
    frame = {**CLEAR_FRAME, "measures": ["rto_rate"], "breakdowns": ["courier"]}
    agent, fakes, _ = make_agent(con, retriever, tmp_path, Script(
        understander=[frame], planner=[PLAN], sql_writer=[{"sql": GOOD_SQL, "explanation": "x"}],
        validator=[{"ok": True}], analyst=[analysis_from]))
    run(agent, "How often do parcels bounce back, per courier?")
    writer = [u for a, _, u in fakes[0].prompts if a == "sql_writer"][0]
    assert "Reference queries with the same tables, columns and metrics" in writer and "rto_at" in writer
    agent2, fakes2, _ = make_agent(con, retriever, tmp_path, Script(
        understander=[frame], planner=[PLAN], sql_writer=[{"sql": GOOD_SQL, "explanation": "x"}],
        validator=[{"ok": True}], analyst=[analysis_from]))
    run(agent2, "How often do parcels bounce back, per courier?", exclude_examples=["lib-del-02", "h06", "m06"])
    writer2 = [u for a, _, u in fakes2[0].prompts if a == "sql_writer"][0]
    assert "Which couriers have the most parcels returned to origin?" not in writer2


# ----------------------------------------------------------------------------- read-back confirmation
def test_readback_is_confirmed_before_any_sql_and_yes_costs_no_extra_call(con, retriever, tmp_path):
    from pantree_insights.graph import CONFIRM_QUESTION, CONFIRM_YES

    frame = {**MUMBAI_SELLERS, "measures": ["orders"]}
    agent, fakes, _ = make_agent(con, retriever, tmp_path, Script(
        understander=[frame], planner=[PLAN], sql_writer=[{"sql": WITH_FILTER, "explanation": "x"}],
        validator=[{"ok": True}], analyst=[analysis_from]))
    run_id, events = run(agent, "How many orders came from sellers in Mumbai?", confirm=False)
    pause = events[-1]
    assert pause["type"] == "clarify" and pause["kind"] == "confirm"
    assert "seller's city = Mumbai" in pause["readback"] and "Orders" in pause["readback"]
    assert not any(a == "sql_writer" for a, _, _ in fakes[0].prompts)          # nothing computed yet
    final = resume(agent, run_id, {CONFIRM_QUESTION: CONFIRM_YES}, confirm=False)[-1]
    assert final["status"] == "done"
    assert [a for a, _, _ in fakes[0].prompts].count("understander") == 1       # confirming needs no new reading


def test_a_correction_is_read_again_and_shown_back_again(con, retriever, tmp_path):
    from pantree_insights.graph import CONFIRM_QUESTION, CONFIRM_YES

    def understander(user):
        if "Correction to your reading of the question: I meant the customers' city" in user:
            return {**MUMBAI_SELLERS, "mentions": [{"text": "Mumbai", "concept": "city"}]}
        return MUMBAI_SELLERS

    agent, fakes, _ = make_agent(con, retriever, tmp_path, Script(
        understander=[understander], planner=[PLAN], sql_writer=[{"sql": GOOD_SQL, "explanation": "x"}],
        validator=[{"ok": True}], analyst=[analysis_from]))
    run_id, events = run(agent, "How many orders in Mumbai?", confirm=False)
    assert "seller's city = Mumbai" in events[-1]["readback"]
    again = resume(agent, run_id, {CONFIRM_QUESTION: "I meant the customers' city"}, confirm=False)
    assert again[-1]["kind"] == "confirm" and "customers' / delivery city = Mumbai" in again[-1]["readback"]
    resume(agent, run_id, {CONFIRM_QUESTION: CONFIRM_YES}, confirm=False)
    writer = [u for a, _, u in fakes[0].prompts if a == "sql_writer"][0]
    assert "Filter: sales.ship_city = 'Mumbai'" in writer


def test_no_without_details_makes_the_model_ask_what_to_change(con, retriever, tmp_path):
    from pantree_insights.graph import CONFIRM_NO, CONFIRM_QUESTION

    def understander(user):
        if "did not say what" in user:
            return ask("What would you like me to change?")
        return MUMBAI_SELLERS

    agent, _, _ = make_agent(con, retriever, tmp_path, Script(
        understander=[understander], planner=[PLAN], sql_writer=[{"sql": WITH_FILTER, "explanation": "x"}],
        validator=[{"ok": True}], analyst=[analysis_from]))
    run_id, _ = run(agent, "How many orders in Mumbai?", confirm=False)
    events = resume(agent, run_id, {CONFIRM_QUESTION: CONFIRM_NO}, confirm=False)
    assert events[-1]["kind"] == "clarify"
    assert events[-1]["questions"][0]["question"] == "What would you like me to change?"


# ----------------------------------------------------------------------------- numbers verified at runtime
NET_BY_CATEGORY = {**CLEAR_FRAME, "restated_question": "Net sales by category", "measures": ["net_sales"]}
NET_RIGHT = ("SELECT p.category, SUM(CASE WHEN s.cancelled_at IS NULL THEN s.net_amount ELSE 0 END) AS net_sales "
             "FROM sales s JOIN product_master p ON p.product_id = s.product_id WHERE s.line_type = 'Sale' GROUP BY 1")
NET_WRONG = NET_RIGHT.replace("SUM(CASE WHEN s.cancelled_at IS NULL THEN s.net_amount ELSE 0 END)", "SUM(s.net_amount)")


def test_happy_path_numbers_are_verified_against_the_data(con, retriever, tmp_path):
    agent, _, _ = make_agent(con, retriever, tmp_path, Script(
        planner=[PLAN], sql_writer=[{"sql": GOOD_SQL, "explanation": "x"}], validator=[{"ok": True}],
        analyst=[analysis_from]))
    final = run(agent, "What is revenue by category?")[1][-1]
    assert final["verification"] == {"status": "verified", "metrics": {"realized_revenue": "verified"}}


@pytest.mark.parametrize("milestone", [None, "shipped", "delivered"])
def test_all_time_city_counts_verify_for_the_selected_milestone(con, retriever, tmp_path, milestone):
    frame = {"restated_question": "Count orders by city across all dates", "subjects": ["orders"],
             "measures": ["orders"], "breakdowns": ["city"], "period": "all data", "time_meaning": milestone,
             "mentions": [{"text": city, "concept": "city"} for city in ("Mumbai", "Pune")]}
    sql = ("SELECT ship_city, COUNT(DISTINCT order_id) AS orders FROM sales "
           "WHERE line_type = 'Sale' AND ship_city IN ('Mumbai', 'Pune')")
    if milestone:
        sql += f" AND {milestone}_at IS NOT NULL"
    sql += " GROUP BY 1"
    agent, fakes, _ = make_agent(con, retriever, tmp_path, Script(
        understander=[frame], planner=[{**PLAN, "metrics": ["orders"], "breakdowns": ["city"]}],
        sql_writer=[{"sql": sql, "explanation": "Count the selected orders by city"}],
        validator=[{"ok": True}], analyst=[{"headline": "Order counts by city.", "findings": []}]))
    _, events = run(agent, "Count orders by city across all dates", use_defaults=True)
    final = events[-1]
    assert final["status"] == "done", final
    assert final["attempts"] == 1 and final["verification"]["status"] == "verified"
    prompt = next(u for a, _, u in fakes[0].prompts if a == "sql_writer")
    assert "All data means no date bounds" in prompt
    if milestone:
        assert f"Required event filter: sales.{milestone}_at IS NOT NULL" in prompt
    understood = next(e for e in events if e.get("step") == "understand" and e.get("status") == "done")
    assert understood["detail"]["verify_scope"]["period"] is None


def test_numbers_that_disagree_with_the_definition_are_sent_back(con, retriever, tmp_path):
    """Forgetting to exclude cancelled lines: the recomputation disagrees, the writer gets the numbers."""
    agent, fakes, _ = make_agent(con, retriever, tmp_path, Script(
        understander=[NET_BY_CATEGORY], planner=[PLAN],
        sql_writer=[{"sql": NET_WRONG, "explanation": "x"}, {"sql": NET_RIGHT, "explanation": "fixed"}],
        validator=[{"ok": True}], analyst=[analysis_from]))
    final = run(agent, "Net sales by category?")[1][-1]
    assert final["attempts"] == 2 and final["verification"]["status"] == "verified"
    retry = [u for a, _, u in fakes[0].prompts if a == "sql_writer"][1]
    assert "recomputed from its definition" in retry and "Apparel" in retry


def test_a_measure_that_is_not_a_metric_is_unverified_not_wrong(con, retriever, tmp_path):
    frame = {**CLEAR_FRAME, "measures": ["revenue per active seller"]}
    agent, _, _ = make_agent(con, retriever, tmp_path, Script(
        understander=[frame], planner=[PLAN], sql_writer=[{"sql": GOOD_SQL, "explanation": "x"}],
        validator=[{"ok": True}], analyst=[analysis_from]))
    final = run(agent, "Revenue per active seller by category?")[1][-1]
    assert final["status"] == "done" and final["verification"]["status"] == "unverified"


def test_followup_retrieves_history_then_resolved_intent(con, retriever, tmp_path, monkeypatch):
    queries = []
    original = retriever.retrieve

    def tracked(question, *args, **kwargs):
        queries.append(question)
        return original(question, *args, **kwargs)

    monkeypatch.setattr(retriever, "retrieve", tracked)
    agent, _, _ = make_agent(con, retriever, tmp_path, Script(
        understander=[MUMBAI_SELLERS], planner=[PLAN], sql_writer=[{"sql": WITH_FILTER, "explanation": "x"}],
        validator=[{"ok": True}], analyst=[analysis_from]))
    final = run(agent, "Now count those orders", history="User: Orders from Mumbai sellers", use_defaults=True)[1][-1]
    assert final["status"] == "done"
    assert "Earlier chat: User: Orders from Mumbai sellers" in queries[0]
    assert "Mumbai" in queries[1] and "orders" in queries[1]


def test_unsafe_requests_are_declined_by_the_understanding_step_before_any_sql(con, retriever, tmp_path):
    """Found by the evaluation: with the understanding context the planner answered 6 unsafe requests."""
    frame = {**CLEAR_FRAME, "decline_reason": "Personal contact details are not available; ask for counts by city."}
    agent, fakes, _ = make_agent(con, retriever, tmp_path, Script(
        understander=[frame], planner=[PLAN], sql_writer=[{"sql": GOOD_SQL, "explanation": "x"}],
        validator=[{"ok": True}], analyst=[analysis_from]))
    _, events = run(agent, "Give me the WhatsApp contacts of people who returned shoes.", use_defaults=True)
    assert events[-1]["status"] == "declined" and "counts by city" in events[-1]["message"]
    assert [a for a, _, _ in fakes[0].prompts] == ["understander"]                 # no plan, no SQL
    assert ("understand", "declined") in steps(events)
