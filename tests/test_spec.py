"""v2 compiler: gold answers, every metric x dimension against the independent recompute, safety, validation."""
import sys
from pathlib import Path

import pytest

from pantree_insights.db import run_query
from pantree_insights.guardrails import check_sql
from pantree_insights.recompute import canonical_sql, check_answer
from pantree_insights.spec import Filter, Measure, QuerySpec, compile_spec, default_catalog, validate

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
from run_eval import load_suite  # noqa: E402
from spec_eval import check, load_gold_specs  # noqa: E402

CAT = default_catalog()
GOLD = load_gold_specs()
ITEMS = {f"{i['suite']}/{i['id']}": i for i in load_suite("all") if i["set"] == "answer"}


def test_every_answer_question_has_a_gold_spec():
    assert set(ITEMS) <= set(GOLD), sorted(set(ITEMS) - set(GOLD))


@pytest.mark.parametrize("key", sorted(k for k, v in GOLD.items() if v.get("route") != "template"))
def test_compiled_gold_spec_returns_the_gold_answer(key):
    result = check(ITEMS[key], GOLD[key], CAT)
    assert result["ok"], f"{key}: {result['reason']}\n{result['sql']}"


@pytest.mark.parametrize("key", sorted(k for k, v in GOLD.items() if v.get("route") != "template"))
def test_compiled_sql_passes_the_safety_checks(key):
    sql = compile_spec(QuerySpec.model_validate(GOLD[key]), CAT)
    verdict = check_sql(sql)
    assert verdict.ok, f"{key}: {verdict.errors}\n{sql}"


def _metric_dimension_pairs():
    pairs = []
    for metric in CAT.metrics.metrics:
        table = CAT.source_table(metric)
        subject = CAT.subject_of.get(table)
        if not subject:
            continue
        for concept, entries in CAT.concepts.concepts.items():
            if subject in entries:
                pairs.append((metric, concept))
    return pairs


PAIRS = _metric_dimension_pairs()


@pytest.mark.parametrize(("metric", "concept"), PAIRS, ids=[f"{m}-by-{c}" for m, c in PAIRS])
def test_every_metric_by_every_dimension_agrees_with_the_recompute(metric, concept):
    """Two independent implementations of 'metric split by concept' must give the same numbers."""
    table = CAT.source_table(metric)
    spec = QuerySpec(table=table, measures=[Measure(name="value_out", metric=metric)], dimensions=[concept])
    problems = validate(spec, CAT)
    entry = CAT.concepts.column(concept, CAT.subject_of[table])
    if problems:                                    # refusing is allowed (a join that would inflate rows) ...
        assert any("repeat" in p or "not known" in p for p in problems), problems
        return
    frame = run_query(CAT.con, compile_spec(spec, CAT), max_rows=100_000).frame
    if entry.expression or entry.join:              # ... the recompute has no derived cuts or window joins
        assert not frame.empty
        return
    key = concept
    id_column = CAT.labels.get(entry.column)
    if id_column:                                   # names several IDs share: compare per ID, not per name
        key, by = id_column, {id_column: f"{entry.column.split('.')[0]}.{id_column}"}
    else:
        by = {concept: entry.column}
    reference = canonical_sql(metric, CAT.graph, by=by)
    if reference is None:
        return
    canonical = run_query(CAT.con, reference, max_rows=100_000).frame
    status, found = check_answer(frame[[key, "value_out"]], canonical, [key],
                                 ratio=CAT.metrics.metrics[metric].unit == "ratio", column="value_out")
    assert status == "verified", (metric, concept, found)


# ----------------------------------------------------------------------------- validation answers with questions
def spec(**kw) -> QuerySpec:
    return QuerySpec(**{"table": "sales", "measures": [Measure(name="orders", metric="orders")], **kw})


def test_unknown_metric_value_and_field_are_reported_not_guessed():
    assert validate(QuerySpec(table="sales", measures=[Measure(name="x", metric="profit")]), CAT)
    assert any("no value" in p for p in validate(spec(filters=[Filter(field="city", values=["Atlantis"])]), CAT))
    assert any("not a cut" in p for p in validate(spec(dimensions=["moon_phase"]), CAT))
    assert any("unknown column" in p for p in validate(spec(dimensions=["sales.colour"]), CAT))


def test_a_metric_on_another_table_is_refused():
    problems = validate(QuerySpec(table="customers", measures=[Measure(name="o", metric="orders")]), CAT)
    assert any("measured on sales" in p for p in problems)


def test_aggregates_need_numbers_and_shares_need_a_condition():
    assert validate(QuerySpec(table="sellers", measures=[Measure(name="a", agg="avg", field="sellers.city")]), CAT)
    assert validate(QuerySpec(table="sales", measures=[Measure(name="s", agg="share")]), CAT)


def test_derived_cut_values_are_its_labels():
    assert not validate(spec(filters=[Filter(field="payment_type", values=["Prepaid"])]), CAT)
    assert validate(spec(filters=[Filter(field="payment_type", values=["Card"])]), CAT)


def test_a_join_that_would_repeat_rows_is_refused():
    problems = validate(QuerySpec(table="customers", measures=[Measure(name="c", agg="count")],
                                  dimensions=["sales.payment_method"]), CAT)
    assert any("repeat" in p for p in problems)


def test_period_and_ranking_are_checked():
    from datetime import date

    from pantree_insights.spec import Ranking, TimeSpec
    assert validate(spec(time=TimeSpec(start=date(2026, 2, 1), end=date(2026, 1, 1))), CAT)
    assert validate(spec(ranking=Ranking(by="revenue")), CAT)


def test_a_name_several_ids_share_is_grouped_with_its_id():
    sql = compile_spec(QuerySpec(table="marketing_attribution",
                                 measures=[Measure(name="r", metric="last_click_revenue")],
                                 dimensions=["campaign"]), CAT)
    assert "campaigns.campaign_id AS campaign_id" in sql and "campaigns.campaign_name AS campaign" in sql
