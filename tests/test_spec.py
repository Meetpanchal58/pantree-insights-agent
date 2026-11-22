"""v2 compiler: gold answers, every metric x dimension against the independent recompute, safety, validation."""
import sys
from pathlib import Path

import pytest

from pantree_insights.db import run_query
from pantree_insights.guardrails import check_sql
from pantree_insights.spec import Filter, Measure, QuerySpec, compile_spec, default_catalog, validate

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from reference_sql import canonical_sql, same_numbers  # noqa: E402
from run_eval import load_suite  # noqa: E402
from spec_eval import check, load_gold_specs  # noqa: E402

pytestmark = pytest.mark.data


@pytest.fixture(scope="module")
def CAT(con):
    return default_catalog(con)

GOLD = load_gold_specs()
ITEMS = {f"{i['suite']}/{i['id']}": i for i in load_suite("all") if i["set"] == "answer"}


def test_every_answer_question_has_a_gold_spec():
    assert set(ITEMS) <= set(GOLD), sorted(set(ITEMS) - set(GOLD))


@pytest.mark.parametrize("key", sorted(GOLD))
def test_gold_spec_or_template_returns_the_gold_answer(key, CAT):
    result = check(ITEMS[key], GOLD[key], CAT)
    assert result["ok"], f"{key}: {result['reason']}\n{result['sql']}"


@pytest.mark.parametrize("key", sorted(k for k, v in GOLD.items() if not v.get("template")))
def test_compiled_sql_passes_the_safety_checks(key, CAT):
    sql = compile_spec(QuerySpec.model_validate(GOLD[key]), CAT)
    verdict = check_sql(sql)
    assert verdict.ok, f"{key}: {verdict.errors}\n{sql}"


def _metric_dimension_pairs():
    # Collection reads semantic definitions only; database access belongs in catalog.
    from pantree_insights.semantic import load_concepts, load_metrics, load_schema
    from pantree_insights.spec import Catalog

    schema = load_schema()
    catalog = Catalog(schema, load_metrics(), load_concepts(), graph=None, catalog={})
    pairs = []
    for metric in catalog.metrics.metrics:
        table = catalog.source_table(metric)
        subject = catalog.subject_of.get(table)
        if not subject:
            continue
        for concept, entries in catalog.concepts.concepts.items():
            if subject in entries:
                pairs.append((metric, concept))
    return pairs


PAIRS = _metric_dimension_pairs()


@pytest.mark.parametrize(("metric", "concept"), PAIRS, ids=[f"{m}-by-{c}" for m, c in PAIRS])
def test_every_metric_by_every_dimension_agrees_with_the_recompute(metric, concept, CAT):
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
    problems = same_numbers(frame, canonical, key, "value_out")
    assert not problems, (metric, concept, problems[:3])


# ----------------------------------------------------------------------------- validation answers with questions
def spec(**kw) -> QuerySpec:
    return QuerySpec(**{"table": "sales", "measures": [Measure(name="orders", metric="orders")], **kw})


def test_unknown_metric_value_and_field_are_reported_not_guessed(CAT):
    assert validate(QuerySpec(table="sales", measures=[Measure(name="x", metric="profit")]), CAT)
    assert any("no value" in p for p in validate(spec(filters=[Filter(field="city", values=["Atlantis"])]), CAT))
    assert any("not a cut" in p for p in validate(spec(dimensions=["moon_phase"]), CAT))
    assert any("unknown column" in p for p in validate(spec(dimensions=["sales.colour"]), CAT))


def test_a_metric_on_another_table_is_refused(CAT):
    problems = validate(QuerySpec(table="customers", measures=[Measure(name="o", metric="orders")]), CAT)
    assert any("measured on sales" in p for p in problems)


def test_aggregates_need_numbers_and_shares_need_a_condition(CAT):
    assert validate(QuerySpec(table="sellers", measures=[Measure(name="a", agg="avg", field="sellers.city")]), CAT)
    assert validate(QuerySpec(table="sales", measures=[Measure(name="s", agg="share")]), CAT)


def test_derived_cut_values_are_its_labels(CAT):
    assert not validate(spec(filters=[Filter(field="payment_type", values=["Prepaid"])]), CAT)
    assert validate(spec(filters=[Filter(field="payment_type", values=["Card"])]), CAT)


def test_a_join_that_would_repeat_rows_is_refused(CAT):
    problems = validate(QuerySpec(table="customers", measures=[Measure(name="c", agg="count")],
                                  dimensions=["sales.payment_method"]), CAT)
    assert any("repeat" in p for p in problems)


def test_period_and_ranking_are_checked(CAT):
    from datetime import date

    from pantree_insights.spec import Ranking, TimeSpec
    assert validate(spec(time=TimeSpec(start=date(2026, 2, 1), end=date(2026, 1, 1))), CAT)
    assert validate(spec(ranking=Ranking(by="revenue")), CAT)


def test_a_name_several_ids_share_is_grouped_with_its_id(CAT):
    sql = compile_spec(QuerySpec(table="marketing_attribution",
                                 measures=[Measure(name="r", metric="last_click_revenue")],
                                 dimensions=["campaign"]), CAT)
    assert "campaigns.campaign_id AS campaign_id" in sql and "campaigns.campaign_name AS campaign" in sql


@pytest.mark.parametrize("key", sorted(k for k in GOLD if k in ITEMS))
def test_reading_checks_never_flag_a_gold_reading(key, CAT):
    """The intent and coverage checks guard the model's reading; a correct reading must pass them untouched,
    otherwise the agent asks the user to change a right answer (the main failure in the previous evaluation)."""
    from pantree_insights.db import manifest
    from pantree_insights.interpret.checks import coverage_problems, intent_problems, template_intent_problems
    from pantree_insights.interpret.models import TemplateCall

    data, question, gold = manifest(), ITEMS[key]["question"], GOLD[key]
    if gold.get("template"):
        problems = template_intent_problems(question, TemplateCall(name=gold["template"], **gold.get("params", {})),
                                            CAT, data)
    else:
        spec = QuerySpec.model_validate(gold)
        problems = intent_problems(question, spec, CAT, data["last_day"]) + coverage_problems(spec, None, data)
    assert not problems, f"{key}: {question}\n{problems}"
