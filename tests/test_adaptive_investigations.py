"""Evidence-directed loops and durable retrieval: real SQL, scripted models, no provider spend."""

import pytest

from pantree_insights.graph import Agent
from pantree_insights.llm import FakeLLM
from pantree_insights.memory import Memory
from pantree_insights.spec import QuerySpec
from pantree_insights.store import Store


def plan(dimension="category"):
    return {
        "table": "sales",
        "dimensions": [dimension],
        "measures": [{"name": "revenue", "metric": "realized_revenue"}],
    }


def probe(dimension="brand", **cell):
    return {
        "action": "probe",
        "probes": [
            {
                "hypothesis": "The mix of brands may explain the observed pattern",
                "competing_explanation": "A broad change across brands may explain it instead",
                "evidence": [{"query_index": 0, "row_index": 0, "column": "revenue", **cell}],
                "spec": plan(dimension),
            }
        ],
    }


@pytest.fixture
def analyst(con, tmp_path):
    a = Agent(con=con, memory=Memory(tmp_path / "memory", "small"))
    yield a
    a.close()


def configure(agent, reviews):
    def respond(role, *_):
        if role == "interpreter":
            return {
                "route": "spec",
                "adaptive": True,
                "restated_question": "Investigate revenue differences",
                "spec": plan(),
            }
        if role == "contrast":
            return {"readings": []}
        if role == "investigator":
            return reviews.pop(0)
        return {"headline": "See observed values", "findings": []}

    model = FakeLLM(respond)
    agent.llm_factory = lambda: model
    return model


def start(agent):
    rid, stream = agent.start("Investigate revenue differences across our business")
    initial = list(stream)[-1]
    assert initial["kind"] == "confirm"
    return rid, initial


def test_adaptive_probe_requires_fresh_approval_and_retains_all_evidence(analyst, monkeypatch):
    model = configure(analyst, [probe(), {"action": "stop", "reason": "Sufficient evidence"}])
    calls = []
    query = analyst.query
    monkeypatch.setattr(analyst, "query", lambda sql: calls.append(sql) or query(sql))
    rid, first = start(analyst)
    assert not calls
    second = list(analyst.resume(rid, "go ahead", first["plan_version"]))[-1]
    assert second["kind"] == "confirm" and len(calls) == 1
    assert "Alternative explanation" in second["readback"] and "Unproven hypothesis" in second["readback"]
    stale = list(analyst.resume(rid, "go ahead", first["plan_version"]))[-1]
    assert stale["kind"] == "confirm" and len(calls) == 1
    final = list(analyst.resume(rid, "go ahead", second["plan_version"]))[-1]
    assert final["status"] == "done" and final["grounded"] and len(calls) == 2
    assert len(final["analyses"]) == len(final["confirmed_plan"]) == 2
    assert final["spec"]["dimensions"] == ["category"]
    assert final["result"] == final["analyses"][0]["result"]
    assert len(final["analysis"]["findings"]) == 2
    assert {p["query_index"] for p in final["populations"]} == {0, 1}
    evidence = final["probe_ledger"][0]["evidence"][0]
    first_result = final["analyses"][0]["result"]
    assert evidence["value"] == first_result["data"][0][first_result["columns"].index("revenue")]
    prompts = [p[2] for p in model.prompts if p[0] == "investigator"]
    assert len(prompts) == 2 and "prior_probes" in prompts[1] and "brand" in prompts[1]


@pytest.mark.parametrize(
    "review", [probe(query_index=99), probe(row_index=99), probe(column="invented"), probe("category")]
)
def test_invalid_or_repeated_probe_stops_with_completed_answer(analyst, review):
    configure(analyst, [review])
    rid, first = start(analyst)
    final = list(analyst.resume(rid, "go ahead", first["plan_version"]))[-1]
    assert final["status"] == "done" and final["grounded"]
    assert len(final["analyses"]) == 1 and final["investigation_stop"]


def test_round_budget_stops_before_another_model_call(analyst, monkeypatch):
    monkeypatch.setattr("pantree_insights.investigate.MAX_ROUNDS", 1)
    model = configure(analyst, [])
    rid, first = start(analyst)
    final = list(analyst.resume(rid, "go ahead", first["plan_version"]))[-1]
    assert final["status"] == "done" and "limit reached" in final["investigation_stop"]
    assert not any(p[0] == "investigator" for p in model.prompts)


def test_cancel_next_probe_preserves_completed_evidence(analyst):
    configure(analyst, [probe()])
    rid, first = start(analyst)
    second = list(analyst.resume(rid, "go ahead", first["plan_version"]))[-1]
    final = list(analyst.resume(rid, "stop", second["plan_version"]))[-1]
    assert final["status"] == "cancelled" and len(final["analyses"]) == 1
    assert final["confirmed_plan"][0]["spec"]["dimensions"] == ["category"]
    assert final["populations"][0]["field"] == "category"


def save(store, user, chat, rid, question, snapshot="snapshot-a"):
    store.create_run(rid, user["id"], chat["id"], question, snapshot=snapshot)
    population = {
        "field": "brand",
        "query_index": 0,
        "complete": True,
        "snapshot": snapshot,
        "values": ["ADIDAS"],
        "filter_field": "brand",
        "expression": "product_master.brand_name",
        "table": "sales",
        "time": None,
        "filters": [],
        "measures": [],
        "ranking": None,
    }
    final = {
        "type": "final",
        "run_id": rid,
        "status": "done",
        "grounded": True,
        "confirmed_plan": [{"spec": plan("brand")}],
        "analysis": {"headline": "Observed revenue"},
        "populations": [population],
        "analyses": [],
    }
    store.complete_run(rid, final, [final], {}, "Observed revenue", final)


def test_retrieval_finds_old_run_after_restart_and_enforces_ownership(tmp_path):
    url = f"sqlite:///{tmp_path}/recall.db"
    store = Store(url)
    user, outsider = store.upsert_user("owner@example.test"), store.upsert_user("outsider@example.test")
    chat = store.create_chat(user["id"])
    save(store, user, chat, "abcdef123456", "Footwear refund variance in Delhi")
    for i in range(35):
        save(store, user, chat, f"{i:012x}", "Recent sales by category")
    store.engine.dispose()
    restored = Store(url)
    try:
        recalled = restored.recall(user["id"], chat["id"], "Revisit the earlier footwear refund analysis", "snapshot-a")
        assert recalled["retrieved_evidence"][0]["run_id"] == "abcdef123456"
        assert not recalled["retrieved_evidence"][0]["stale"]
        assert recalled["historical_populations"][0]["values"] == ["ADIDAS"]
        assert not restored.recall(outsider["id"], chat["id"], "Footwear refund", "snapshot-a")["retrieved_evidence"]
        stale = restored.recall(user["id"], chat["id"], "run abcdef123456", "snapshot-b")
        assert stale["retrieved_evidence"][0]["stale"] and not stale["historical_populations"][0]["complete"]
    finally:
        restored.engine.dispose()


def test_historical_reference_is_explicit_and_cannot_replace_latest(analyst):
    from test_contextual_investigations import result_scope

    from pantree_insights.context import ResultReference, bind_population, choose_population

    previous, latest = result_scope(analyst)
    old = {**latest[0], "run_id": "abcdef123456", "values": latest[0]["values"][:2]}
    assert choose_population("these brands", [*latest, old]) is latest[0]
    chosen = choose_population(
        "the earlier brands", [*latest, old], ResultReference(run_id="abcdef123456", query_index=0, field="brand")
    )
    result = bind_population(
        QuerySpec.model_validate(plan("brand")), chosen, analyst.catalog, analyst.snapshot, "the earlier brands"
    )
    assert result.filters[-1].values == old["values"] and result.time == previous.time


def test_three_rounds_stop_and_preserve_each_query_chart(analyst):
    configure(analyst, [probe("brand"), probe("city")])
    rid, reply = start(analyst)
    for _ in range(3):
        reply = list(analyst.resume(rid, "go ahead", reply["plan_version"]))[-1]
    assert reply["status"] == "done" and len(reply["analyses"]) == 3
    assert all(item.get("chart") for item in reply["analyses"])
    assert "limit reached" in reply["investigation_stop"]


def test_spend_threshold_stops_additional_planning(analyst, monkeypatch):
    monkeypatch.setattr("pantree_insights.investigate.MAX_USD", 0)
    model = configure(analyst, [])
    rid, first = start(analyst)
    final = list(analyst.resume(rid, "go ahead", first["plan_version"]))[-1]
    assert final["status"] == "done" and "budget reached" in final["investigation_stop"]
    assert not any(p[0] == "investigator" for p in model.prompts)


def test_retrieval_cannot_cross_chats_or_invent_a_run(tmp_path):
    store = Store(f"sqlite:///{tmp_path}/isolation.db")
    user = store.upsert_user("owner@example.test")
    first, second = store.create_chat(user["id"]), store.create_chat(user["id"])
    try:
        save(store, user, first, "abcdef123456", "Footwear refund variance")
        assert not store.recall(user["id"], second["id"], "run abcdef123456 footwear", "snapshot-a")[
            "retrieved_evidence"
        ]
        assert not store.recall(user["id"], first["id"], "run ffffffffffff", "snapshot-a")["retrieved_evidence"]
    finally:
        store.engine.dispose()


def test_probe_retains_original_filters_and_period(analyst):
    from pantree_insights.investigate import retain_scope
    from pantree_insights.spec import SpecError

    baseline = {
        **plan(),
        "filters": [{"field": "brand", "values": ["ADIDAS"]}],
        "time": {"start": "2026-10-01", "end": "2026-10-31"},
    }
    proposal = QuerySpec.model_validate({**plan("product"), "time": {"start": "2026-01-01", "end": "2026-11-01"}})
    fixed = retain_scope(proposal, baseline, analyst.catalog)
    assert str(fixed.time.start) == "2026-10-01" and fixed.filters[0].values == ["ADIDAS"]
    proposal = QuerySpec.model_validate({**plan("product"), "filters": [{"field": "brand", "values": ["Nike"]}]})
    with pytest.raises(SpecError, match="approved population"):
        retain_scope(proposal, baseline, analyst.catalog)


def test_unavailable_or_stale_historical_scope_never_executes(analyst):
    from test_contextual_investigations import result_scope

    from pantree_insights.interpret import decide

    _, scopes = result_scope(analyst)
    old = {**scopes[0], "run_id": "abcdef123456", "snapshot": "old"}
    model = FakeLLM(
        lambda *_: {
            "route": "spec",
            "restated_question": "Older brands",
            "spec": plan("brand"),
            "result_scope": {"run_id": old["run_id"], "query_index": 0, "field": "brand"},
        }
    )
    decision = decide(
        model,
        analyst.catalog,
        {},
        {},
        analyst.data,
        "Revisit the earlier analysis of these brands",
        investigation={"populations": scopes, "historical_populations": [old]},
        snapshot=analyst.snapshot,
    )
    assert decision.kind == "clarify" and "different snapshot" in decision.questions[0].question
