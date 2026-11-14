"""The agent flow end to end on the small shop, with a scripted fake LLM (no network, no cost)."""
import json

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


def analysis_from(user_prompt: str, wrong: bool = False) -> dict:
    """An analyst answer that quotes the first number of the result table (or a made-up one)."""
    table = user_prompt.split("## Result table\n", 1)[1].split("\n\n", 1)[0].splitlines()
    first_row = table[1].split(",")
    value = "123456789" if wrong else first_row[1]
    return {"headline": f"{first_row[0]} leads with Rs {value} realized revenue.",
            "findings": [f"{first_row[0]} brings Rs {value}."], "recommendations": ["Keep investing."],
            "memory": [{"statement": f"{first_row[0]} is the top category by realized revenue", "type": "fact"}],
            "chart_title": "Realized revenue by category", "chart_metric": "realized_revenue"}


class Script:
    """Scripted answers per agent; each agent pops from its own list (the last answer repeats)."""

    def __init__(self, **answers):
        self.answers = {k: list(v) for k, v in answers.items()}

    def __call__(self, agent, schema, system, user):
        queue = self.answers[agent]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        return item(user) if callable(item) else item


@pytest.fixture(scope="module")
def retriever(con):
    from pantree_insights.retrieval import Retriever

    return Retriever(con)


def make_agent(con, retriever, tmp_path, script):
    from pantree_insights.db import manifest
    from pantree_insights.graph import Agent

    fakes = []

    def factory():
        fakes.append(FakeLLM(script))
        return fakes[-1]

    memory = Memory(tmp_path / "memory", data_size="small")
    agent = Agent(llm_factory=factory, retriever=retriever, memory=memory, con=con,
                  data=manifest(retriever_shop(con)))
    return agent, fakes, memory


def retriever_shop(con):
    from pantree_insights.config import ROOT
    return ROOT / "data" / "small"


def run(agent, question, **kw):
    run_id, events = agent.start(question, **kw)
    return run_id, list(events)


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
        "check_input", "retrieve", "plan", "write_sql", "check_sql", "run_query", "validate", "analyse",
        "chart", "remember"]
    assert final["chart"]["kind"] in ("bar", "bar_h")
    assert final["result"]["columns"] == ["category", "realized_revenue", "orders"]
    assert final["warnings"] == []
    assert final["cost"]["calls"] == 4
    saved = memory.insights()
    assert len(saved) == 1 and saved[0]["sql"].startswith("SELECT")
    # the planner saw the retrieved context; the writer saw the plan
    agents_called = [a for a, _, _ in fakes[0].prompts]
    assert agents_called == ["planner", "sql_writer", "validator", "analyst"]
    assert "## Tables" in fakes[0].prompts[0][2] and "Realized revenue by category" in fakes[0].prompts[1][2]
    json.dumps(final)                                                  # everything is JSON for the web app


def test_guardrail_and_database_errors_are_fed_back(con, retriever, tmp_path):
    agent, fakes, _ = make_agent(con, retriever, tmp_path, Script(
        planner=[PLAN],
        sql_writer=[{"sql": "SELECT * FROM sales", "explanation": "x"},
                    {"sql": "SELECT no_such_column, COUNT(*) FROM sales GROUP BY 1", "explanation": "x"},
                    {"sql": GOOD_SQL, "explanation": "ok"}],
        validator=[{"ok": True}], analyst=[analysis_from]))
    _, events = run(agent, "What is revenue by category?")
    assert events[-1]["status"] == "done" and events[-1]["attempts"] == 3
    assert ("check_sql", "retry") in steps(events) and ("run_query", "retry") in steps(events)
    writer_prompts = [u for a, _, u in fakes[0].prompts if a == "sql_writer"]
    assert "SELECT * is not allowed" in writer_prompts[1]
    assert "no_such_column" in writer_prompts[2]


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


def test_clarifying_questions_pause_and_resume(con, retriever, tmp_path):
    clarify = {"decision": "clarify", "restated_question": "Sales by category",
               "clarifying_questions": [{"question": "Which sales measure?", "options": ["Realized revenue", "Orders"],
                                         "default": "Realized revenue", "why": "changes the ranking"}]}

    def planner(user):
        return PLAN if "answers to your clarifying questions" in user else clarify

    agent, fakes, _ = make_agent(con, retriever, tmp_path, Script(
        planner=[planner], sql_writer=[{"sql": GOOD_SQL, "explanation": "x"}],
        validator=[{"ok": True}], analyst=[analysis_from]))
    run_id, events = run(agent, "How are sales by category?")
    assert events[-1]["type"] == "clarify" and events[-1]["questions"][0]["default"] == "Realized revenue"
    resumed = list(agent.resume(run_id, {"Which sales measure?": "Realized revenue"}))
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


def test_unsupported_numbers_trigger_a_rewrite(con, retriever, tmp_path):
    answers = [lambda u: analysis_from(u, wrong=True), analysis_from]
    agent, _, _ = make_agent(con, retriever, tmp_path, Script(
        planner=[PLAN], sql_writer=[{"sql": GOOD_SQL, "explanation": "x"}], validator=[{"ok": True}],
        analyst=answers))
    _, events = run(agent, "What is revenue by category?")
    analyse_done = [e for e in events if e.get("step") == "analyse" and e["status"] == "done"][0]
    assert analyse_done["detail"]["rewritten"] and analyse_done["detail"]["number_check"] == "passed"
    assert events[-1]["warnings"] == []
