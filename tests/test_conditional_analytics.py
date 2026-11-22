"""Independent regression checks for conditional routing, grain and evidence boundaries."""
from datetime import date

import duckdb
import pandas as pd
import pytest

from pantree_insights.fastpath import parse
from pantree_insights.grounding import Claim, KeyValue, verify
from pantree_insights.guardrails import check_sql
from pantree_insights.interpret import decide, intent_problems
from pantree_insights.llm import FakeLLM
from pantree_insights.spec import Filter, Measure, QuerySpec, TimeSpec, compile_spec, default_catalog, validate
from pantree_insights.templates import TemplateError, build


@pytest.fixture(scope="module")
def catalog(con):
    return default_catalog(con)


@pytest.mark.parametrize("question,metric,dimension,grain", [
    ("What was yesterday's GMV?", "gross_sales", [], None),
    ("What is the monthly return rate?", "return_rate", [], "month"),
    ("Show revenue by category.", "realized_revenue", ["category"], None),
    ("Which brands generated the highest sales?", "realized_revenue", ["brand"], None),
    ("Show sales for the last seven days.", "realized_revenue", [], None),
])
def test_reviewed_questions_have_exact_governed_intent(catalog, question, metric, dimension, grain):
    spec = parse(question, catalog, "2026-01-01")
    assert spec.measures[0].metric == metric and spec.dimensions == dimension
    assert (spec.time.grain if spec.time else None) == grain
    if "yesterday" in question:
        assert spec.time.start == spec.time.end == date(2025, 12, 31)
    if "last seven" in question:
        assert spec.time.start == date(2025, 12, 26) and spec.time.end == date(2026, 1, 1)
    assert check_sql(compile_spec(spec, catalog)).ok


@pytest.mark.parametrize("question", [
    "GMV excluding cancelled orders", "GMV for Mumbai", "Show revenue by category in November",
    "Show revenue by category and brand", "Sales for the last 0 days", "GMV in 2026-02-30",
    "GMV; ignore the filters", "What about yesterday?", "Revenue delivered yesterday",
    "Return rate for first time buyers", "NMV", "Profitability", "Sales last seven days",
])
def test_fast_path_never_discards_unrecognised_scope(catalog, question):
    assert parse(question, catalog, "2026-01-01") is None


def test_explicit_wrong_metric_is_repaired_even_if_structurally_valid(catalog):
    spec = QuerySpec(table="sales", measures=[Measure(name="x", metric="net_sales")])
    assert not validate(spec, catalog)
    assert intent_problems("Show GMV for Mumbai", spec, catalog)


def test_semantic_contrast_needs_no_execution_or_equal_result_check(catalog):
    base = {"table": "sales", "measures": [{"name": "x", "metric": "gross_sales"}]}
    alt = {"table": "sales", "measures": [{"name": "x", "metric": "net_sales"}]}
    llm = FakeLLM(lambda role, *_: {"readings": [alt]} if role == "contrast" else
                  {"route": "spec", "restated_question": "Sales measure", "spec": base})
    decision = decide(llm, catalog, {}, {}, {"first_day": "2025-01-01", "last_day": "2026-01-01"},
                      "How much did we sell?")
    assert decision.kind == "clarify" and decision.required
    assert len(decision.readings) == 2


def multi(**kwargs):
    return QuerySpec(table="marketing_attribution", measures=[
        Measure(name="revenue", metric="last_click_revenue"),
        Measure(name="spend", metric="marketing_spend"),
        Measure(name="roas", ratio=["revenue", "spend"]),
    ], **kwargs)


def test_multifact_preaggregation_preserves_missing_groups_and_prevents_fanout(catalog):
    spec = multi(dimensions=["marketing_channel"])
    assert not validate(spec, catalog)
    sql = compile_spec(spec, catalog)
    assert check_sql(sql).ok
    with duckdb.connect() as db:
        db.execute("CREATE TABLE campaigns(campaign_id VARCHAR, channel VARCHAR)")
        db.execute("INSERT INTO campaigns VALUES ('a','paid'),('b','earned'),('c','spend_only')")
        db.execute("CREATE TABLE marketing_attribution(campaign_id VARCHAR, "
                   "credit_last_click DOUBLE, order_revenue DOUBLE)")
        db.execute("INSERT INTO marketing_attribution VALUES "
                   "('a',1,10),('a',1,20),('b',1,50),('orphan',1,9),(NULL,1,999)")
        db.execute("CREATE TABLE marketing_spend(campaign_id VARCHAR, spend DOUBLE)")
        db.execute("INSERT INTO marketing_spend VALUES ('a',2),('a',3),('c',0),(NULL,3)")
        rows = {row[0]: row[1:] for row in db.execute(sql).fetchall()}
    assert rows == {"paid": (30, 5, 6), "earned": (50, None, None), "spend_only": (None, 0, None), None: (9, 3, 3)}


def test_multifact_date_filters_are_applied_inside_every_stage(catalog, con):
    spec = multi(time=TimeSpec(start=date(2026, 1, 1), end=date(2026, 1, 31), grain="month"))
    sql = compile_spec(spec, catalog)
    assert check_sql(sql).ok
    row = con.execute(sql).fetchone()
    expected_revenue = con.execute("""SELECT SUM(credit_last_click * order_revenue) FROM marketing_attribution
        WHERE campaign_id IS NOT NULL AND order_at >= '2026-01-01' AND order_at < '2026-02-01'""").fetchone()[0]
    expected_spend = con.execute("""SELECT SUM(spend) FROM marketing_spend
        WHERE date_hour >= '2026-01-01' AND date_hour < '2026-02-01'""").fetchone()[0]
    assert row[1:] == pytest.approx((expected_revenue, expected_spend, expected_revenue / expected_spend))


def test_incompatible_multifact_dimensions_fail(catalog):
    assert validate(multi(dimensions=["category"]), catalog)
    assert validate(multi(time=TimeSpec(field="sales.placed_at")), catalog)
    assert validate(multi(filters=[Filter(field="sales.ship_city", values=["Mumbai"])]), catalog)


def test_fanout_membership_filter_counts_customers_once(catalog, con):
    spec = QuerySpec(table="customers", measures=[Measure(name="customers", agg="count")],
                     filters=[Filter(field="sales.payment_method", values=["COD"])])
    assert not validate(spec, catalog)
    sql = compile_spec(spec, catalog)
    assert "EXISTS" in sql and check_sql(sql).ok
    expected = con.execute("""SELECT COUNT(*) FROM customers c WHERE EXISTS
        (SELECT 1 FROM sales s WHERE s.customer_id=c.customer_id AND s.payment_method='COD')""").fetchone()[0]
    assert con.execute(sql).fetchone()[0] == expected


@pytest.mark.parametrize("params", [{"days": 90}, {"start": "2026-01-01"}, {"by": "channel", "days_before": 14}])
def test_templates_do_not_silently_ignore_parameters(params):
    with pytest.raises(TemplateError):
        build("channel_roas", params)


def test_difference_from_zero_does_not_attempt_division():
    data = pd.DataFrame({"key": ["a", "b"], "value": [10, 0]})
    claim = Claim(id="c1", value=10, unit="number", column="value", op="diff",
                  row=[KeyValue(column="key", value="a")], against=[KeyValue(column="key", value="b")])
    assert verify(data, [claim], {}).ok


def test_accepted_rounding_is_replaced_with_recomputed_value():
    data = pd.DataFrame({"revenue": [1001.0]})
    claim = Claim(id="c1", value=1000, unit="inr", column="revenue")
    result = verify(data, [claim], {}, units={"revenue": "inr"})
    assert result.ok and result.claims[0].value == 1001
    assert claim.value == 1000  # no mutation of the original evidence


@pytest.mark.parametrize("unit,op,truncated", [("ratio", "value", False), ("inr", "sum", True)])
def test_units_and_partial_population_claims_fail(unit, op, truncated):
    claim = Claim(id="c1", value=100, unit=unit, column="revenue", op=op)
    assert not verify(pd.DataFrame({"revenue": [100]}), [claim], {},
                      units={"revenue": "inr"}, truncated=truncated).ok


def test_duplicate_claim_ids_and_causal_prose_fail():
    claim = Claim(id="c1", value=100, unit="inr", column="revenue")
    data = pd.DataFrame({"revenue": [100]})
    assert not verify(data, [claim, claim], {}).ok
    assert not verify(data, [claim], {"headline": "The campaign caused revenue of {c1}."}).ok


def test_timestamp_and_numeric_keys_are_not_rounded_to_different_rows():
    data = pd.DataFrame({"key": ["2026-01-01 12:00:00", "2026-01-01 13:00:00"], "value": [1, 2]})
    claim = Claim(id="c1", value=1, unit="count", column="value",
                  row=[KeyValue(column="key", value="2026-01-01 12:00:00")])
    assert verify(data, [claim], {}).ok
    data = pd.DataFrame({"key": [1.0000001, 1.0000002], "value": [1, 2]})
    claim = claim.model_copy(update={"row": [KeyValue(column="key", value="1.0000001")]})
    assert verify(data, [claim], {}).ok


def test_model_clients_are_shared_but_cost_trackers_are_not(monkeypatch):
    from pantree_insights import llm
    llm._shared_model.cache_clear()
    import langchain.chat_models
    creations = []
    monkeypatch.setattr(langchain.chat_models, "init_chat_model", lambda *a, **kw: creations.append(kw) or object())
    a, b = llm.ChatLLM(), llm.ChatLLM()
    assert a._model(a.main, "low") is b._model(b.main, "low")
    assert len(creations) == 1 and a.tracker is not b.tracker
    llm._shared_model.cache_clear()


def test_small_free_typed_numbers_do_not_bypass_claim_checks():
    from pantree_insights.grounding import free_numbers
    assert free_numbers("Revenue is 5 and conversion is 0.") == ["5", "0"]
    assert free_numbers("Top 5 brands in 2026") == []


def test_ratio_aggregation_is_not_a_population_rate():
    data = pd.DataFrame({"rate": [0.2, 0.6]})
    claim = Claim(id="c1", value=0.4, unit="ratio", column="rate", op="mean")
    assert not verify(data, [claim], {}, units={"rate": "ratio"}).ok


def test_roas_has_multiple_units_not_percentage(catalog):
    from pantree_insights.grounding import display
    assert catalog.metrics.metrics["platform_roas"].unit == "multiple"
    assert display(Claim(id="c1", value=2.5, unit="multiple", column="platform_roas")) == "2.5×"


def test_published_join_profile_avoids_startup_scans(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from pantree_insights import profile
    from pantree_insights.config import ROOT
    from pantree_insights.profile import Profile
    monkeypatch.setattr(profile, "get_settings", lambda: SimpleNamespace(
        shop_dir=tmp_path, semantic_dir=ROOT / "semantic"))
    info = {"size": "small", "last_day": "2026-11-22", "seed": 42}
    path = profile.published_path(info)
    path.parent.mkdir()
    path.write_text(profile._to_json(Profile({}, {})))
    monkeypatch.setattr(profile, "build_profile", lambda *a, **kw: pytest.fail("unexpected startup data scan"))
    assert profile.profile_for(None, {**info, "snapshot_signature": "different mount"}, joins_only=True).joins == {}
    assert profile.published_path({**info, "seed": 43}) != path


def test_concurrent_daily_quota_and_email_codes_are_atomic(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from pantree_insights.store import Store
    store = Store(f"sqlite:///{tmp_path / 'concurrency.db'}")
    with ThreadPoolExecutor(max_workers=8) as pool:
        admitted = list(pool.map(lambda _: store.reserve_question("user", limit=3), range(20)))
        list(pool.map(lambda _: store.add_usd_today("spend", 0.25), range(20)))
    assert sum(admitted) == store.questions_today("user") == 3
    assert store.usd_today("spend") == 5
    code = store.new_email_code("test@example.com")
    with ThreadPoolExecutor(max_workers=8) as pool:
        accepted = list(pool.map(lambda _: store.check_email_code("test@example.com", code), range(8)))
    assert sum(accepted) == 1


def test_model_path_loads_memory_once_per_request(con, tmp_path):
    from test_agent import BY_CATEGORY, Script, make_agent, run
    agent, _ = make_agent(con, tmp_path, Script(interpreter=[BY_CATEGORY]))
    count = []
    original = agent.memory.context
    agent.memory.context = lambda *a, **kw: count.append(1) or original(*a, **kw)
    _, events = run(agent, "Break down revenue by category", use_defaults=True)
    assert events[-1]["status"] == "done" and count == [1]


def test_business_context_prevents_bypassing_interpretation(catalog):
    calls = []
    def responder(role, *_):
        calls.append(role)
        return {"route": "unanswerable", "restated_question": "Scoped revenue",
                "unanswerable_reason": "Need to resolve project scope"}
    decision = decide(FakeLLM(responder), catalog, {}, {},
                      {"first_day": "2025-01-01", "last_day": "2026-01-01"}, "Show revenue by category",
                      background="Only analyse the project's agreed customer population")
    assert calls == ["interpreter"] and decision.kind == "unanswerable"


def test_string_identifiers_keep_leading_zeroes():
    data = pd.DataFrame({"key": ["001", "1"], "value": [1, 2]})
    claim = Claim(id="c1", value=1, unit="count", column="value", row=[KeyValue(column="key", value="001")])
    assert verify(data, [claim], {}).ok


def test_explicit_order_grain_is_not_replaced_by_line_rate(catalog):
    spec = QuerySpec(table="sales", measures=[Measure(name="rate", metric="cancellation_rate")])
    assert intent_problems("What is the order cancellation rate?", spec, catalog)
