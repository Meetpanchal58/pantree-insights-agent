"""Evaluation runner logic without LLM calls: judging, failure types, decline layer, reports."""
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
import ablation_report  # noqa: E402
import run_eval  # noqa: E402


def test_load_suite_has_all_sets():
    items = run_eval.load_suite("holdout")
    sets = {i["set"] for i in items}
    assert {"answer", "clarify", "decline", "false_decline", "adversarial", "overask"} <= sets
    assert len([i for i in items if i["set"] == "false_decline"]) >= 15
    assert len([i for i in items if i["set"] == "adversarial"]) >= 10
    assert all(i["author"] in ("self", "external") for i in items)


def test_decline_layer_and_behaviour_judging():
    guard = [{"type": "step", "step": "check_input", "status": "blocked"}]
    planner = [{"type": "step", "step": "plan", "status": "done", "detail": {"decision": "decline"}}]
    declined = {"type": "final", "status": "declined"}
    assert run_eval.judge(None, {"set": "adversarial"}, declined, guard)["decline_layer"] == "input_guard"
    assert run_eval.judge(None, {"set": "adversarial"}, declined, planner)["passed"]
    fd = run_eval.judge(None, {"set": "false_decline"}, declined, guard)
    assert not fd["passed"] and fd["failure_type"] == "declined_wrongly"
    oa = run_eval.judge(None, {"set": "overask"}, {"type": "clarify"}, [])
    assert not oa["passed"] and oa["failure_type"] == "over_asked"


@pytest.mark.data
def test_result_and_answer_correctness_against_gold(con):
    item = {"set": "answer", "gold_sql": "SELECT payment_method, COUNT(DISTINCT order_id) AS orders FROM sales "
                                         "WHERE line_type = 'Sale' GROUP BY 1",
            "key_columns": ["payment_method"], "metric_columns": ["orders"]}
    gold = con.execute(item["gold_sql"]).df()
    top = gold.sort_values("orders").iloc[-1]
    claim = {"id": "c1", "value": float(top.orders), "unit": "count", "column": "n_orders",
             "row": [{"column": "method", "value": top.payment_method}], "op": "value"}
    agent_sql = ("SELECT payment_method AS method, COUNT(DISTINCT order_id) AS n_orders FROM sales "
                 "WHERE line_type = 'Sale' GROUP BY 1")
    final = {"type": "final", "status": "done", "sql": agent_sql, "grounded": True, "analysis": {"claims": [claim]}}
    ok = run_eval.judge(con, item, final, [])
    assert ok["result_correct"] and ok["answer_correct"]
    wrong = {**claim, "value": float(top.orders) * 1.3}
    bad = run_eval.judge(con, item, {**final, "analysis": {"claims": [wrong]}}, [])
    assert bad["result_correct"] and not bad["answer_correct"] and bad["failure_type"] == "claims_unverified"
    missing = run_eval.judge(con, item, {**final, "sql": agent_sql + " HAVING COUNT(*) > 99999999"}, [])
    assert missing["failure_type"] in ("missing_rows", "wrong_dimension")


def fake_rows(variant, pattern):
    return [{"suite": "holdout", "variant": variant, "repeat": r, "model": "m", "temperature": None, "id": q,
             "author": "self", "set": "answer", "expect": "answer", "tuned": False, "question": q, "seconds": 10.0,
             "status": "done", "cost": 0.01, "attempts": 1, "grounded": True, "passed": v, "result_correct": v,
             "answer_correct": v, "decline_layer": None, "failure_type": None if v else "wrong_metric_value",
             "reason": "x"} for q, vals in pattern.items() for r, v in enumerate(vals)]


def test_summary_and_ablation_report(tmp_path):
    full = fake_rows("full", {"q1": [True, True], "q2": [True, False], "q3": [True, True]})
    base = fake_rows("baseline", {"q1": [False, False], "q2": [True, False], "q3": [True, False]})
    text = run_eval.summary(full)
    assert "Correct result tables" in text and "pass^2" in text and "provider default" in text
    for name, rows in [("f.json", full), ("b.json", base)]:
        (tmp_path / name).write_text(json.dumps(rows))
    table = ablation_report.build([tmp_path / "f.json", tmp_path / "b.json"])
    assert "| baseline |" in table and "| full |" in table and "pairs=6" in table
    assert pd.notna(len(table))


def test_answer_question_without_a_result_counts_as_wrong_not_missing():
    """An answer question with no SQL, a decline or a clarification is a wrong result, not an excluded one."""
    item = {"set": "answer"}
    for final in ({"type": "final", "status": "failed", "message": "Could not produce a valid query after 3 attempts"},
                  {"type": "final", "status": "declined"}, {"type": "clarify"}):
        out = run_eval.judge(None, item, final, [])
        assert out["result_correct"] is False and out["answer_correct"] is False, final
    for kind in ("false_decline", "overask"):                 # behaviour sets have no result measure
        out = run_eval.judge(None, {"set": kind}, {"type": "final", "status": "declined"}, [])
        assert out["result_correct"] is None


def test_transient_api_error_is_retried_then_recorded_not_crashed():
    """A provider error (e.g. HTTP 502) must not end the whole run: retry once, then return None."""
    class Flaky:
        def __init__(self, failures):
            self.failures, self.calls = failures, 0

        def start(self, question, **kwargs):
            self.calls += 1
            if self.calls <= self.failures:
                def boom():
                    raise RuntimeError("Error code: 502")
                    yield
                return "run", boom()
            return "run", iter([{"type": "final", "status": "done"}])

    item = {"question": "q", "set": "answer"}
    once = Flaky(1)
    assert run_eval.ask(once, item, wait=0) == [{"type": "final", "status": "done"}] and once.calls == 2
    always = Flaky(5)
    assert run_eval.ask(always, item, wait=0) is None and always.calls == 2


def test_provider_errors_are_listed_but_not_scored():
    base = {"suite": "holdout", "variant": "full", "repeat": 0, "model": "m", "temperature": None, "author": "self",
            "set": "answer", "tuned": False, "question": "q", "seconds": 1.0, "cost": 0.01, "attempts": 1,
            "grounded": True, "decline_layer": None}
    ok = {**base, "id": "a1", "status": "done", "result_correct": True, "answer_correct": True, "passed": True,
          "failure_type": None, "reason": "match"}
    err = {**base, "id": "a2", "status": "api_error", "result_correct": None, "answer_correct": None,
           "passed": None, "failure_type": "api_error", "reason": "provider error after retry (not scored)"}
    text = run_eval.summary([ok, err])
    assert "| SQL execution success | 100% [100%-100%] (n=1) |" in text
    assert "api_error 1" in text and "a2 [api_error]" in text


def test_claims_in_percent_points_hold_on_a_ratio_gold_column():
    gold = pd.DataFrame({"prime_share": [0.102012]})
    claims = [{"id": "c1", "value": 10.2, "unit": "number", "column": "prime_share_pct", "row": []}]
    ok, _ = run_eval.claims_hold_on_gold(claims, gold, {"prime_share_pct": "prime_share"})
    assert ok
    bad = [{"id": "c1", "value": 12.0, "unit": "number", "column": "prime_share_pct", "row": []}]
    assert not run_eval.claims_hold_on_gold(bad, gold, {"prime_share_pct": "prime_share"})[0]


def test_rescore_keeps_provider_errors_unscored(tmp_path, monkeypatch):
    row = {"suite": "holdout", "variant": "full", "repeat": 0, "model": "m", "temperature": None, "id": "x02",
           "author": "self", "set": "answer", "tuned": False, "question": "q", "seconds": 1.0, "cost": 0.0,
           "attempts": 0, "status": "api_error", "sql": None, "grounded": None, "claims": None,
           "result_correct": None, "answer_correct": None, "passed": None, "decline_layer": None,
           "failure_type": "api_error", "reason": "provider error after retry (not scored)"}
    path = tmp_path / "run.json"
    path.write_text(json.dumps([row]))
    monkeypatch.setattr(run_eval, "connect", lambda: None)
    run_eval.rescore(path)
    saved = json.loads(path.read_text())[0]
    assert saved["result_correct"] is None and saved["failure_type"] == "api_error"


def test_a_readback_confirmation_is_not_a_clarifying_question():
    confirm = {"type": "clarify", "kind": "confirm"}
    vague = run_eval.judge(None, {"set": "clarify"}, confirm, [])
    assert not vague["passed"] and vague["failure_type"] == "did_not_clarify"
    clear = run_eval.judge(None, {"set": "overask"}, confirm, [])
    assert clear["passed"] and clear["failure_type"] is None
    asked = run_eval.judge(None, {"set": "clarify"}, {"type": "clarify", "kind": "clarify"}, [])
    assert asked["passed"]


def test_decline_by_the_understanding_step_is_recognised():
    events = [{"type": "step", "step": "understand", "status": "declined", "detail": {"decision": "decline"}}]
    assert run_eval.decline_layer(events) == "understanding"
