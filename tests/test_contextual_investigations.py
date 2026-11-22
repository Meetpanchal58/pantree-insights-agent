"""General regressions for result-bound drilldowns, compiler identities and planner failure recovery."""

from datetime import date

import pytest

from pantree_insights.context import bind_population, populations
from pantree_insights.graph import Agent
from pantree_insights.interpret import decide, normalise
from pantree_insights.llm import FakeLLM
from pantree_insights.memory import Memory
from pantree_insights.spec import Measure, QuerySpec, Ranking, SpecError, TimeSpec, compile_spec, validate


@pytest.fixture
def analyst(con, tmp_path):
    agent = Agent(con=con, memory=Memory(tmp_path, "small"))
    yield agent
    agent.close()


def result_scope(agent, count=5, dimensions=None):
    previous = QuerySpec(
        table="sales",
        measures=[Measure(name="revenue", metric="realized_revenue"), Measure(name="orders", metric="orders")],
        dimensions=dimensions or ["brand"],
        time=TimeSpec(start=date(2026, 10, 16), end=date(2026, 11, 14)),
        ranking=Ranking(by="revenue", limit=count),
    )
    frame = agent.con.execute(compile_spec(previous, agent.catalog)).df()
    split = {"columns": list(frame.columns), "data": frame.values.tolist(), "truncated": False}
    return previous, populations([{"spec": previous.model_dump(mode="json")}], [split], agent.catalog, agent.snapshot)


@pytest.mark.parametrize(
    "dimensions",
    [
        ["product", "product_master.product_id"],
        ["product_master.product_id", "product"],
        ["product", "product_master.product_name", "product_master.product_id"],
        ["seller", "sellers.seller_id"],
        ["category", "product_master.category"],
    ],
)
def test_implicit_and_explicit_identity_keys_compile_once(analyst, dimensions):
    spec = QuerySpec(
        table="sales", measures=[Measure(name="revenue", metric="realized_revenue")], dimensions=dimensions
    )
    spec = normalise(spec, analyst.catalog, {})
    assert validate(spec, analyst.catalog) == []
    frame = analyst.con.execute(compile_spec(spec, analyst.catalog)).df()
    assert len(frame.columns) == len(set(frame.columns))
    expected = analyst.con.execute(
        "SELECT SUM(CASE WHEN delivered_at IS NOT NULL THEN net_amount ELSE 0 END) FROM sales WHERE line_type='Sale'"
    ).fetchone()[0]
    assert frame["revenue"].sum() <= expected  # realized revenue subtracts refunds


def test_measure_alias_repair_preserves_metric_ranking_and_ratio_references(analyst):
    spec = QuerySpec(
        table="sales",
        dimensions=["category"],
        measures=[
            Measure(name="category", metric="realized_revenue"),
            Measure(name="orders", metric="orders"),
            Measure(name="ratio", ratio=["category", "orders"]),
        ],
        ranking=Ranking(by="category"),
    )
    fixed = normalise(spec, analyst.catalog, {})
    assert fixed.measures[0].metric == "realized_revenue"
    assert fixed.ranking.by == fixed.measures[0].name == fixed.measures[2].ratio[0]
    assert not validate(fixed, analyst.catalog)


@pytest.mark.parametrize("count", [5, 12])
def test_followup_uses_complete_ranked_population_and_inherited_metric_period(analyst, count):
    previous, scopes = result_scope(analyst, count)
    model = FakeLLM(
        lambda role, *_: (
            {"readings": []}
            if role == "contrast"
            else {
                "route": "spec",
                "restated_question": "Category and product revenue for the earlier brands",
                "spec": {
                    "table": "sales",
                    "dimensions": ["brand", "category", "product", "product_master.product_id"],
                    "measures": [{"name": "product_id", "metric": "realized_revenue"}],
                },
            }
        )
    )
    outcome = decide(
        model,
        analyst.catalog,
        {},
        {},
        analyst.data,
        "These brands in which category are performing well along with products?",
        register={"performing": ["Realized revenue", "Orders", "Conversion rate"]},
        previous=previous,
        investigation={"populations": scopes},
        snapshot=analyst.snapshot,
    )
    assert outcome.kind == "spec"
    assert outcome.spec.time == previous.time
    assert set(outcome.spec.filters[-1].values) == set(scopes[0]["values"])
    assert len(outcome.spec.filters[-1].values) == count
    assert outcome.spec.measures[0].metric == "realized_revenue"
    assert outcome.spec.ranking is None  # a breakdown does not reuse the earlier top-N as a global row limit
    frame = analyst.con.execute(compile_spec(outcome.spec, analyst.catalog)).df()
    assert set(frame["brand"]) == set(scopes[0]["values"])
    assert model.tracker.summary()["calls"] <= 2


def test_cross_fact_product_population_aggregates_each_source_without_fanout(analyst):
    _, scopes = result_scope(analyst, 5, ["product"])
    spec = QuerySpec(
        table="sales",
        dimensions=["product"],
        measures=[Measure(name="revenue", metric="realized_revenue"), Measure(name="rating", metric="avg_rating")],
    )
    bound = bind_population(
        spec, scopes[0], analyst.catalog, analyst.snapshot, "These products across sales and reviews"
    )
    assert not validate(bound, analyst.catalog)
    combined = analyst.con.execute(compile_spec(bound, analyst.catalog)).df().set_index("product_id")
    for metric, table in [("revenue", "sales"), ("rating", "product_reviews")]:
        single = bound.model_copy(update={"table": table, "measures": [m for m in bound.measures if m.name == metric]})
        expected = analyst.con.execute(compile_spec(single, analyst.catalog)).df().set_index("product_id")
        for key, value in expected[metric].items():
            if value == value:
                assert combined.loc[key, metric] == pytest.approx(value)


@pytest.mark.parametrize("corruption", ["truncated", "incomplete", "stale", "null"])
def test_partial_stale_or_null_result_groups_cannot_silently_become_filters(analyst, corruption):
    previous, scopes = result_scope(analyst)
    scope = scopes[0]
    if corruption in ("truncated", "incomplete", "null"):
        scope["complete"] = False
    else:
        scope["snapshot"] = "old"
    with pytest.raises(SpecError, match="incomplete|different snapshot"):
        bind_population(previous, scope, analyst.catalog, analyst.snapshot, "these brands")


def test_internal_planner_failure_is_not_a_request_to_rephrase(analyst):
    model = FakeLLM(
        lambda *_: {
            "restated_question": "Revenue",
            "spec": {"table": "sales", "measures": [{"name": "revenue", "metric": "nonexistent_metric"}]},
        }
    )
    outcome = decide(model, analyst.catalog, {}, {}, analyst.data, "Evaluate the sales opportunity", contrast=False)
    assert outcome.kind == "failed" and "not a problem with your wording" in outcome.reason
    assert len(model.prompts) == 2 and "previous proposed structure" in model.prompts[1][2]


def test_repeated_answered_clarification_never_emits_an_empty_chat_reply(analyst):
    model = FakeLLM(
        lambda *_: {
            "route": "clarify",
            "restated_question": "Revenue",
            "questions": [{"question": "Which measure?", "options": ["Revenue"], "default": "Revenue", "why": "Scope"}],
        }
    )
    analyst.llm_factory = lambda: model
    rid, stream = analyst.start("Investigate our trading")
    first = list(stream)[-1]
    final = list(analyst.resume(rid, "Realized revenue", first["plan_version"]))[-1]
    assert final["type"] == "final" and final["status"] == "failed" and final["message"].strip()


def test_unrelated_performance_question_still_requests_a_measure(analyst):
    model = FakeLLM(lambda *_: pytest.fail("Register should handle unresolved independent ambiguity"))
    outcome = decide(
        model,
        analyst.catalog,
        {},
        {},
        analyst.data,
        "Which categories are performing well?",
        register={"performing": ["Realized revenue", "Orders"]},
    )
    assert outcome.kind == "clarify" and outcome.questions


@pytest.mark.parametrize(
    "metrics",
    [
        [("revenue", "realized_revenue")],
        [("revenue", "realized_revenue"), ("rating", "avg_rating")],
    ],
)
def test_top_products_within_each_brand_ranks_after_aggregation(analyst, metrics):
    spec = QuerySpec(
        table="sales",
        dimensions=["brand", "product"],
        measures=[Measure(name=name, metric=metric) for name, metric in metrics],
        ranking=Ranking(by="revenue", limit=3, partition_by=["brand"]),
    )
    from pantree_insights.guardrails import check_sql

    sql = compile_spec(spec, analyst.catalog)
    assert check_sql(sql).ok
    top = analyst.con.execute(sql).df()
    all_groups = analyst.con.execute(compile_spec(spec.model_copy(update={"ranking": None}), analyst.catalog)).df()
    for brand, rows in top.groupby("brand"):
        expected = all_groups[all_groups.brand == brand].revenue.sort_values(ascending=False).head(3).tolist()
        assert rows.revenue.tolist() == pytest.approx(expected)
    assert len(top) > 3 and top.groupby("brand").size().max() == 3


def test_root_cause_contributions_reconcile_to_observed_period_change(analyst):
    from pantree_insights.spec import Operation

    spec = QuerySpec(
        table="sales",
        dimensions=["category"],
        measures=[Measure(name="revenue", metric="realized_revenue")],
        time=TimeSpec(start=date(2026, 10, 1), end=date(2026, 10, 14)),
        operation=Operation(kind="period_compare", previous_start=date(2026, 9, 1), previous_end=date(2026, 9, 14)),
        shares=["revenue"],
    )
    breakdown = analyst.con.execute(compile_spec(spec, analyst.catalog)).df()
    total = analyst.con.execute(
        compile_spec(spec.model_copy(update={"dimensions": [], "shares": []}), analyst.catalog)
    ).df()
    assert breakdown.revenue_delta.sum() == pytest.approx(total.revenue_delta.iloc[0])
    assert breakdown.revenue_contribution.sum() == pytest.approx(1)
    # This proves descriptive attribution of the change, not that a category caused it.


def test_complete_populations_survive_store_restart_and_short_prose_history(analyst, tmp_path):
    from pantree_insights.store import Store

    previous, scopes = result_scope(analyst, 12)
    store = Store(f"sqlite:///{tmp_path}/populations.db")
    user = store.upsert_user("scope@example.test")
    other = store.upsert_user("outsider@example.test")
    chat = store.create_chat(user["id"])
    store.create_run("scope", user["id"], chat["id"], "Top brands", snapshot=analyst.snapshot)
    final = {
        "status": "done",
        "grounded": True,
        "question": "Top brands",
        "confirmed_plan": [{"spec": previous.model_dump(mode="json")}],
        "populations": scopes,
        "analyses": [],
        "analysis": {"headline": "A short summary showing only some brands"},
    }
    store.complete_run("scope", final, [], {}, "Short summary", final)
    for _ in range(10):
        store.add_message(chat["id"], "user", "A later message")
    restarted = Store(str(store.engine.url))
    try:
        context = restarted.investigation(user["id"], chat["id"])
        assert len(context["populations"][0]["values"]) == 12
        assert restarted.investigation(other["id"], chat["id"]) == {}
        assert "Top brands" not in restarted.conversation(chat["id"])[0]
    finally:
        restarted.engine.dispose()
        store.engine.dispose()


def test_explicit_subset_and_exclusions_intersect_the_recorded_population(analyst):
    from pantree_insights.spec import Filter

    previous, scopes = result_scope(analyst)
    brand = scopes[0]["values"][0]
    requested = previous.model_copy(update={"filters": [Filter(field="brand", op="not_in", values=[brand])]})
    bound = bind_population(requested, scopes[0], analyst.catalog, analyst.snapshot, f"these brands except {brand}")
    frame = analyst.con.execute(compile_spec(bound, analyst.catalog)).df()
    assert brand not in set(frame.brand)
    assert set(frame.brand) == set(scopes[0]["values"]) - {brand}


def test_hierarchical_ranking_still_requires_explicit_confirmation(analyst, monkeypatch):
    from pantree_insights.interpret import Decision

    analyst.llm_factory = lambda: FakeLLM(lambda *_: {"headline": "See the result table", "findings": []})
    spec = QuerySpec(
        table="sales",
        dimensions=["brand", "product"],
        measures=[Measure(name="revenue", metric="realized_revenue")],
        ranking=Ranking(by="revenue", limit=2, partition_by=["brand"]),
    )
    monkeypatch.setattr(
        "pantree_insights.graph.decide",
        lambda *a, **kw: Decision(
            "spec", spec=spec, restated="Top two products by revenue within each brand", detail={"fast_path": True}
        ),
    )
    queries = []
    query = analyst.query
    monkeypatch.setattr(analyst, "query", lambda sql: queries.append(sql) or query(sql))
    rid, stream = analyst.start("Top two products per brand")
    proposal = list(stream)[-1]
    assert proposal["kind"] == "confirm" and not queries
    assert "within each brand" in proposal["readback"]
    final = list(analyst.resume(rid, "Go ahead", proposal["plan_version"]))[-1]
    assert final["status"] == "done" and final["grounded"] and len(queries) == 1


def test_legacy_excerpt_cannot_be_reused_as_the_whole_population(analyst):
    from pantree_insights.context import saved_populations

    previous, _ = result_scope(analyst, 12)
    frame = analyst.con.execute(compile_spec(previous, analyst.catalog)).df()
    legacy = {
        "evidence": [
            {
                "plan": [{"spec": previous.model_dump(mode="json")}],
                "snapshot": analyst.snapshot,
                "results": [
                    {
                        "columns": list(frame.columns),
                        "data": frame.values.tolist()[:6],
                        "row_count": len(frame),
                        "excerpted": True,
                    }
                ],
            }
        ]
    }
    restored = saved_populations(legacy, analyst.catalog)
    assert restored and not restored[0]["complete"] and not restored[0]["values"]


@pytest.mark.parametrize("grounded,truncated", [(False, False), (True, True)])
def test_latest_ineligible_result_invalidates_previous_reference(analyst, tmp_path, grounded, truncated):
    from pantree_insights.context import saved_populations
    from pantree_insights.store import Store

    previous, scopes = result_scope(analyst)
    store = Store(f"sqlite:///{tmp_path}/latest.db")
    user = store.upsert_user("scope@example.test")
    chat = store.create_chat(user["id"])
    try:
        first = {
            "status": "done",
            "grounded": True,
            "confirmed_plan": [{"spec": previous.model_dump(mode="json")}],
            "populations": scopes,
            "analyses": [],
        }
        store.create_run("first", user["id"], chat["id"], "Top brands")
        store.complete_run("first", first, [], {}, "Earlier complete result", first)
        latest = {
            **first,
            "grounded": grounded,
            "populations": [],
            "analyses": [{"result": {"columns": ["brand"], "data": [["partial"]], "truncated": truncated}}],
        }
        store.create_run("latest", user["id"], chat["id"], "A new result")
        store.complete_run("latest", latest, [], {}, "Latest result is incomplete", latest)
        context = store.investigation(user["id"], chat["id"])
        assert context["evidence"] and saved_populations(context, analyst.catalog) == []
    finally:
        store.engine.dispose()


def test_same_named_dimensions_keep_separate_meanings_when_ranked(analyst):
    spec = QuerySpec(
        table="sales",
        dimensions=["sellers.city", "customers.city", "brand"],
        measures=[Measure(name="revenue", metric="realized_revenue")],
        ranking=Ranking(by="revenue", limit=2, partition_by=["sellers.city"]),
    )
    frame = analyst.con.execute(compile_spec(spec, analyst.catalog)).df()
    assert {"sellers_city", "customers_city"} <= set(frame.columns)
    assert frame.groupby("sellers_city").size().max() == 2
    scopes = populations(
        [{"spec": spec.model_dump(mode="json")}],
        [{"columns": list(frame.columns), "data": frame.values.tolist()}],
        analyst.catalog,
        analyst.snapshot,
    )
    assert {s["expression"] for s in scopes} >= {"sellers.city", "customers.city"}


def test_explicit_and_implicit_product_identity_create_one_reference(analyst):
    _, scopes = result_scope(analyst, 5, ["product", "product_master.product_id"])
    assert len(scopes) == 1 and scopes[0]["field"] == "product"


def test_product_words_are_not_misidentified_as_other_brands():
    import pandas as pd

    from pantree_insights.grounding import Claim, KeyValue, verify

    frame = pd.DataFrame(
        [
            {"brand": "2go", "product": "2go Red T-shirt", "revenue": 12},
            {"brand": "Red", "product": "Red Jacket", "revenue": 24},
        ]
    )
    claim = Claim(
        id="c1",
        column="revenue",
        value=12,
        unit="inr",
        row=[KeyValue(column="brand", value="2go"), KeyValue(column="product", value="2go Red T-shirt")],
    )
    assert verify(frame, [claim], {"headline": "2go, 2go Red T-shirt: revenue = {c1}"}).ok
    assert not verify(frame, [claim], {"headline": "Red: revenue = {c1}"}).ok


def test_period_edit_does_not_require_a_prior_result_population(analyst):
    from pantree_insights.context import refers_to_results

    assert not refers_to_results("same for yesterday")
    previous, _ = result_scope(analyst)
    previous = previous.model_copy(update={"ranking": None})
    outcome = decide(
        FakeLLM(lambda *_: pytest.fail("A reviewed period edit needs no model")),
        analyst.catalog,
        {},
        {},
        analyst.data,
        "same for yesterday",
        previous=previous,
    )
    assert outcome.kind == "spec" and outcome.spec.time != previous.time


def test_dimension_alias_normalisation_preserves_partition_ranking(analyst):
    spec = QuerySpec(
        table="sales",
        dimensions=["brand", "product_master.brand_name", "product"],
        measures=[Measure(name="revenue", metric="realized_revenue")],
        ranking=Ranking(by="revenue", limit=2, partition_by=["product_master.brand_name"]),
    )
    fixed = normalise(spec, analyst.catalog, {})
    assert fixed.ranking.partition_by == ["brand"]
    assert not validate(fixed, analyst.catalog)


def test_screenshot_followup_runs_through_confirmation_and_grounding(analyst):
    previous, scopes = result_scope(analyst)

    def reply(role, *_):
        if role == "interpreter":
            return {
                "route": "spec",
                "restated_question": "Category and product revenue for these brands",
                "spec": {
                    "table": "sales",
                    "dimensions": ["brand", "category", "product", "product_master.product_id"],
                    "measures": [{"name": "product_id", "metric": "realized_revenue"}],
                },
            }
        if role == "contrast":
            return {"readings": []}
        return {"headline": "See the result table", "findings": []}

    analyst.llm_factory = lambda: FakeLLM(reply)
    rid, stream = analyst.start(
        "These brands in which category are performing well along with products?",
        previous_spec=previous.model_dump(mode="json"),
        investigation={"populations": scopes},
    )
    proposal = list(stream)[-1]
    assert proposal["kind"] == "confirm"
    assert proposal["spec"]["time"] == previous.time.model_dump(mode="json")
    final = list(analyst.resume(rid, "Go ahead", proposal["plan_version"]))[-1]
    assert final["status"] == "done" and final["grounded"]
    result = final["result"]
    assert not result["truncated"]
    assert {row[result["columns"].index("brand")] for row in result["data"]} == set(scopes[0]["values"])
    assert any(p["field"] == "product" and p["complete"] for p in final["populations"])


def test_alternative_readings_cannot_drop_the_exact_result_population(analyst):
    previous, scopes = result_scope(analyst)
    primary = {
        "table": "sales",
        "dimensions": ["brand", "category"],
        "measures": [{"name": "revenue", "metric": "realized_revenue"}],
    }
    alternative = {**primary, "dimensions": ["brand", "product"]}
    model = FakeLLM(
        lambda role, *_: (
            {"readings": [alternative], "why": ["Breakdown level"]}
            if role == "contrast"
            else {"route": "spec", "restated_question": "Revenue breakdown for these brands", "spec": primary}
        )
    )
    decision = decide(
        model,
        analyst.catalog,
        {},
        {},
        analyst.data,
        "Break down revenue for these brands",
        previous=previous,
        investigation={"populations": scopes},
        snapshot=analyst.snapshot,
    )
    assert decision.kind == "clarify" and len(decision.readings) == 2
    for reading in decision.readings:
        assert reading.time == previous.time
        assert any(f.field == "brand" and set(f.values) == set(scopes[0]["values"]) for f in reading.filters)


@pytest.mark.parametrize("period", ["all data", "previous month", "Q3"])
def test_explicit_new_reporting_period_is_not_replaced_by_previous_scope(analyst, period):
    previous, scopes = result_scope(analyst)
    current = previous.model_copy(
        update={"time": None if period == "all data" else TimeSpec(start=date(2026, 9, 1), end=date(2026, 9, 30))}
    )
    bound = bind_population(current, scopes[0], analyst.catalog, analyst.snapshot, f"these brands for {period}")
    assert bound.time == current.time
