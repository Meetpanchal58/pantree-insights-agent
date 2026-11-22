"""The evaluation runner: suites, the simulated stakeholder, judging and the release gate (no model calls)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
import run_eval  # noqa: E402

from pantree_insights.graph import CONFIRM_QUESTION, CONFIRM_YES  # noqa: E402

pytestmark = pytest.mark.data


def test_load_suite_has_every_set_and_a_gold_spec_for_every_answer():
    items = run_eval.load_suite("all")
    assert {i["set"] for i in items} == {"answer", "clarify", "decline", "false_decline", "adversarial", "overask"}
    specs = run_eval.gold_specs()
    assert all(f"{i['suite']}/{i['id']}" in specs for i in items if i["set"] == "answer")


def test_stakeholder_confirms_only_the_intended_reading():
    from pantree_insights.spec import default_catalog
    intent = {"table": "sales", "measures": [{"name": "r", "metric": "realized_revenue"}], "dimensions": ["category"]}
    who = run_eval.Stakeholder(intent, default_catalog())
    same = {"kind": "confirm", "spec": {**intent, "measures": [{"name": "rev", "metric": "realized_revenue"}]}}
    other = {"kind": "confirm", "spec": {**intent, "dimensions": ["brand"]}}
    assert who.answer(same) == {CONFIRM_QUESTION: CONFIRM_YES}
    assert who.answer(other)[CONFIRM_QUESTION].startswith("No. What I want is")


def test_stakeholder_picks_the_option_matching_its_intent():
    from pantree_insights.spec import default_catalog
    intent = {"table": "sales", "measures": [{"name": "r", "metric": "return_rate"}],
              "dimensions": ["category", "size_fit"]}
    who = run_eval.Stakeholder(intent, default_catalog())
    question = {"kind": "clarify", "questions": [{"question": "What do you mean by 'wrong size'?", "options": [
        "The item did not fit: delivered lines with size fit Too Small or Too Large (sales.size_fit)",
        "The return reason given was Size Issue (sales.return_reason)"]}]}
    assert "size fit" in who.answer(question)["What do you mean by 'wrong size'?"]


def test_judging_behaviour_sets():
    asked = {"type": "clarify", "kind": "clarify"}
    confirm = {"type": "clarify", "kind": "confirm", "spec": {"table": "sales"}}
    declined = {"type": "final", "status": "declined"}
    assert run_eval.judge(None, {"set": "clarify"}, asked, {})["passed"]
    assert not run_eval.judge(None, {"set": "clarify"}, confirm, {})["passed"]
    assert run_eval.judge(None, {"set": "decline"}, declined, {})["passed"]
    assert run_eval.judge(None, {"set": "overask"}, confirm, {})["passed"]
    assert run_eval.judge(None, {"set": "false_decline"}, declined, {})["failure_type"] == "declined_wrongly"


@pytest.mark.parametrize("kind", ["overask", "false_decline"])
@pytest.mark.parametrize("status", ["failed", "api_error", "running", "done"])
def test_failed_and_empty_behaviour_runs_do_not_pass(kind, status):
    assert not run_eval.judge(None, {"set": kind}, {"type": "final", "status": status}, {})["passed"]


def test_gate_requires_every_case_to_pass():
    assert not run_eval.passes_gate([], 0)
    assert not run_eval.passes_gate([{"passed": False, "set": "overask"}], 1)
    assert not run_eval.passes_gate([{"passed": None}], 1)
    assert not run_eval.passes_gate([{"passed": True}], 2)
    assert run_eval.passes_gate([{"passed": True}], 1)
    assert not run_eval.passes_gate([{"passed": True, "failure_type": "silent_wrong"}], 1)


def test_repeated_wrong_confirmations_are_bounded():
    class Agent:
        def start(self, *args, **kwargs):
            return "run", iter([{"type": "clarify", "kind": "confirm"}])

        def resume(self, *args, **kwargs):
            return iter([{"type": "clarify", "kind": "confirm"}])

    class User:
        def matches(self, event):
            return False

        def answer(self, event):
            return {"confirm": "wrong"}

    events, turns = run_eval.ask(Agent(), {"set": "answer", "question": "q"}, User(), mode="assisted")
    assert len(events) == run_eval.MAX_TURNS + 1
    assert turns["corrections"] == turns["interactions"] == run_eval.MAX_TURNS


def test_a_wrong_verified_answer_is_a_silent_wrong_and_fails_the_gate(con):
    item = {"set": "answer", "question": "Orders?", "gold_sql": "SELECT COUNT(DISTINCT order_id) AS orders "
                                                               "FROM sales WHERE line_type = 'Sale'"}
    wrong = {"type": "final", "status": "done", "trust": "verified", "sql": "SELECT COUNT(*) AS orders FROM sales"}
    right = {**wrong, "sql": item["gold_sql"]}
    bad = run_eval.judge(con, item, wrong, {})
    assert bad["failure_type"] == "silent_wrong" and run_eval.judge(con, item, right, {})["passed"]
    rows = [{**bad, "set": "answer"}]
    assert not run_eval.passes_gate(rows, 1)


def test_summary_reports_the_gate_measures():
    rows = [{"suite": "holdout", "set": "answer", "id": "a", "question": "q", "status": "done", "trust": "verified",
             "passed": True, "result_correct": True, "failure_type": None, "reason": "match", "questions": 0,
             "first_reading": True, "seconds": 3.0, "cost": 0.01, "model": "m", "code_revision": "abc1234"}]
    text = run_eval.summary(rows)
    assert "Silent wrong answers (gate 0) | 0" in text and "Verified answers equal to the gold" in text


@pytest.mark.parametrize("provider_error", [False, True])
def test_live_runner_repeats_and_counts_failures(monkeypatch, tmp_path, provider_error):
    import httpx
    from openai import APITimeoutError

    from pantree_insights import graph

    class Agent:
        def start(self, *args, **kwargs):
            if provider_error:
                raise APITimeoutError(request=httpx.Request("POST", "https://example.test"))
            raise TimeoutError("simulated provider timeout")

        def close(self):
            pass

    monkeypatch.setattr(graph, "Agent", Agent)
    item = next(i for i in run_eval.load_suite("flow") if i["set"] == "answer")
    rows = run_eval.run([item], tmp_path / "partial.jsonl", repeats=2)
    assert [r["repeat"] for r in rows] == [0, 1]
    kind = "api_error" if provider_error else "execution_error"
    assert all(r["passed"] is False and r["failure_type"] == kind and r["cost"] is None for r in rows)
    assert all(r["snapshot_verified"] and r["data_sha256"] and r["cases_sha256"] for r in rows)
    assert not run_eval.passes_gate(rows, 2)
    assert ("Provider errors | 2" if provider_error else "Execution errors | 2") in run_eval.summary(rows)
    import json
    report = tmp_path / "report.json"
    report.write_text(json.dumps(rows))
    original = report.read_bytes()
    run_eval.rescore(report)
    assert report.read_bytes() == original
    assert report.with_name("report.rescored.json").exists()


def test_first_turn_metric_includes_answers_that_skip_confirmation():
    class Agent:
        def start(self, *args, **kwargs):
            return "run", iter([{"type": "final", "status": "done", "spec": {"table": "sales"}}])
    class User:
        def matches(self, event):
            return True
    _, turns = run_eval.ask(Agent(), {"set": "answer", "question": "q"}, User())
    assert turns["first_turn_correct"] and turns["first_turn_answered"] and turns["first_reading"]
    assert turns["interactions"] == 0


def test_autonomous_evaluation_never_receives_gold_corrections():
    class Agent:
        def start(self, *args, **kwargs):
            return "run", iter([{"type": "clarify", "kind": "confirm", "spec": {"table": "sales"}}])
        def resume(self, *args, **kwargs):
            assert args[1] == "Go ahead"
            return iter([{"type": "final", "status": "done"}])
    class Judge:
        def matches(self, event):
            return False
        def answer(self, event):
            raise AssertionError("gold may judge, never steer an autonomous answer")
    events, turns = run_eval.ask(Agent(), {"set": "answer", "question": "q"}, Judge())
    assert len(events) == 2 and not turns["first_turn_correct"] and turns["confirmations"] == 1
    assert turns["first_turn_plan_ready"] and turns["confirmation_compliant"]
