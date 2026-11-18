"""Evaluation questions must be unseen: nothing the model reads (prompts, semantic files) may contain them."""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _questions(name: str, key: str = "questions") -> list[dict]:
    return yaml.safe_load((ROOT / "evals" / name).read_text())[key] or []


EVAL = (_questions("holdout.yaml") + _questions("behaviour.yaml") + _questions("coverage.yaml")
        + _questions("questions.yaml") + _questions("flow_regression.yaml") + _questions("regression.yaml", "examples"))
MODEL_READS = [p.read_text(encoding="utf-8").lower() for p in [*(ROOT / "prompts").glob("*.yaml"),
                                                              *(ROOT / "semantic").rglob("*.yaml")]]


def test_ids_are_unique_per_file():
    for name in ("holdout.yaml", "behaviour.yaml", "coverage.yaml", "questions.yaml", "flow_regression.yaml"):
        ids = [q["id"] for q in _questions(name)]
        assert len(ids) == len(set(ids)), name


def test_no_evaluation_question_is_in_anything_the_model_reads():
    for q in EVAL:
        text = q["question"].lower()
        assert not any(text in t for t in MODEL_READS), q["id"]


def test_gold_specs_are_never_in_the_semantic_layer():
    assert not any("gold_specs" in t for t in MODEL_READS)
    assert not (ROOT / "semantic" / "gold_specs.yaml").exists()


def test_holdout_answers_are_fully_annotated():
    needed = {"required_tables", "gold_sql", "key_columns", "metric_columns"}
    for q in _questions("holdout.yaml") + _questions("coverage.yaml"):
        if q["expect"] == "answer":
            assert needed <= set(q), f"{q['id']} missing {needed - set(q)}"


def test_coverage_questions_differ_from_the_other_sets():
    words = lambda t: set(re.findall(r"[a-z0-9]+", t.lower()))  # noqa: E731
    others = [q["question"] for q in EVAL if q not in _questions("coverage.yaml")]
    for q in _questions("coverage.yaml"):
        for o in others:
            a, b = words(q["question"]), words(o)
            assert len(a & b) / len(a | b) <= 0.6, (q["id"], o)
