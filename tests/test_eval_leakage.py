"""The holdout must be unseen: not in examples, the regression set, the glossary or the prompts."""
import re
from pathlib import Path

import yaml

from pantree_insights.semantic import load_examples

ROOT = Path(__file__).resolve().parents[1]
HOLDOUT = yaml.safe_load((ROOT / "evals" / "holdout.yaml").read_text())["questions"]
REGRESSION = yaml.safe_load((ROOT / "evals" / "questions.yaml").read_text())["questions"]


def words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def jaccard(a: str, b: str) -> float:
    wa, wb = words(a), words(b)
    return len(wa & wb) / len(wa | wb)


def test_holdout_ids_and_questions_are_unique():
    ids = [q["id"] for q in HOLDOUT]
    assert len(ids) == len(set(ids))
    assert {"answer", "clarify", "decline"} <= {q["expect"] for q in HOLDOUT}


def test_holdout_not_in_examples_or_regression():
    seen = [e.question for e in load_examples().examples] + [q["question"] for q in REGRESSION]
    for q in HOLDOUT:
        close = [s for s in seen if jaccard(q["question"], s) > 0.6]
        assert not close, f"{q['id']} is too close to a seen question: {close}"


def test_holdout_gold_sql_not_in_examples():
    example_sql = {re.sub(r"\s+", " ", e.sql.strip().lower()) for e in load_examples().examples}
    for q in HOLDOUT:
        if q.get("gold_sql"):
            assert re.sub(r"\s+", " ", q["gold_sql"].strip().lower()) not in example_sql, q["id"]


def test_holdout_not_in_prompts_or_semantic_files():
    texts = [p.read_text(encoding="utf-8").lower() for p in [*(ROOT / "prompts").glob("*.yaml"),
                                                            *(ROOT / "semantic").glob("*.yaml")]]
    for q in HOLDOUT:
        assert not any(q["question"].lower() in t for t in texts), q["id"]


def test_holdout_answers_are_fully_annotated():
    needed = {"intent", "required_tables", "required_metrics", "required_joins", "filter_values", "result_grain",
              "gold_sql", "key_columns", "metric_columns"}
    for q in HOLDOUT:
        if q["expect"] == "answer":
            assert needed <= set(q), f"{q['id']} missing {needed - set(q)}"
