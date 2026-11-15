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
