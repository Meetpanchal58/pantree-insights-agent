"""The query library (semantic/library/): every reference query is safe, runs, and does not leak evaluation answers."""
import re
import sys
from pathlib import Path

import pandas as pd
import pytest
import yaml

from pantree_insights.db import run_query
from pantree_insights.guardrails import check_sql
from pantree_insights.semantic import load_examples, load_library

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))
from library_coverage import analyse, check_metrics  # noqa: E402

LIBRARY = load_library()
AREAS = {"sales", "customers", "funnel", "marketing", "inventory", "delivery", "support"}
HOLDOUT = (yaml.safe_load((ROOT / "evals" / "holdout.yaml").read_text())["questions"]
           + (yaml.safe_load((ROOT / "evals" / "holdout_external.yaml").read_text())["questions"] or [])
           + yaml.safe_load((ROOT / "evals" / "behaviour.yaml").read_text())["questions"])
REGRESSION = yaml.safe_load((ROOT / "evals" / "questions.yaml").read_text())["questions"]


def words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def jaccard(a: str, b: str) -> float:
    wa, wb = words(a), words(b)
    return len(wa & wb) / len(wa | wb)


def test_ids_unique_across_examples_and_library():
    ids = [e.id for e in load_examples().examples] + [e.id for e in LIBRARY]
    assert len(ids) == len(set(ids))
    assert {e.area for e in LIBRARY} <= AREAS


@pytest.mark.parametrize("entry", LIBRARY, ids=lambda e: e.id)
def test_passes_guardrails_and_metric_claims_hold(entry):
    verdict = check_sql(entry.sql)
    assert verdict.ok, verdict.errors
    assert not check_metrics(entry, analyse(entry.sql))
    assert entry.variants, "give at least one other phrasing"


def test_questions_do_not_leak_evaluation_questions():
    seen = [q["question"] for q in HOLDOUT + REGRESSION] + [e.question for e in load_examples().examples]
    for e in LIBRARY:
        for text in [e.question, *e.variants]:
            close = [s for s in seen if jaccard(text, s) > 0.6]
            assert not close, f"{e.id}: '{text}' is too close to {close}"


def _fingerprint(frame: pd.DataFrame) -> frozenset:
    """Order- and name-independent fingerprint of a result: its rows as sets of rounded values."""
    def norm(v):
        if isinstance(v, float):
            return round(v, 4)
        return str(v).lower()
    return frozenset(frozenset(norm(v) for v in row) for row in frame.itertuples(index=False))


@pytest.mark.data
def test_results_run_have_rows_and_no_duplicate_rows(con):
    for e in LIBRARY:
        frame = run_query(con, check_sql(e.sql).sql).frame
        assert len(frame) > 0, f"{e.id}: no rows on the small shop"
        assert not frame.duplicated().any(), f"{e.id}: duplicate rows"


@pytest.mark.data
def test_results_do_not_reproduce_an_evaluation_answer(con):
    """A library query must not return the same table as a holdout gold query or a verified example: that
    would hand the agent the answer to a question it is scored on."""
    scored = {q["id"]: q["gold_sql"] for q in HOLDOUT if q.get("gold_sql")}
    scored |= {e.id: e.sql for e in load_examples().examples}
    gold = {k: _fingerprint(run_query(con, check_sql(sql).sql).frame) for k, sql in scored.items()}
    # a single zero or empty cell (e.g. "orders before the shop opened" = 0) is in many results by chance
    gold = {k: fp for k, fp in gold.items() if not (len(fp) == 1 and next(iter(fp)) <= {0, "0", "none", "nan"})}
    leaks = []
    for e in LIBRARY:
        mine = _fingerprint(run_query(con, check_sql(e.sql).sql).frame)
        # leak: every row of a scored answer is inside some row of the library result (extra columns don't hide it)
        same = [k for k, fp in gold.items() if fp and all(any(g <= m for m in mine) for g in fp)]
        if same:
            leaks.append(f"{e.id} contains the answer to {same}")
    assert not leaks, leaks
