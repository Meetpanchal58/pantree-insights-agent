"""Independent fixtures for analytical populations, conversation scope and evidence mapping."""
from datetime import date

import duckdb
import pandas as pd
import pytest

from pantree_insights.fastpath import followup, independent_history, parse
from pantree_insights.grounding import Claim, KeyValue, verify
from pantree_insights.guardrails import check_sql
from pantree_insights.interpret import canonical, intent_problems, readback
from pantree_insights.spec import Measure, QuerySpec, compile_spec, default_catalog, validate
from pantree_insights.templates import TemplateError, build


@pytest.fixture(scope="module")
def cat(con):
    return default_catalog(con)


@pytest.fixture
def sample():
    with duckdb.connect() as db:
        db.execute("""CREATE TABLE sales(customer_id VARCHAR, order_id VARCHAR, product_id VARCHAR,
            placed_at TIMESTAMP, line_type VARCHAR, gross_amount DOUBLE, net_amount DOUBLE,
            delivered_at TIMESTAMP, refund_amount DOUBLE, cancelled_at TIMESTAMP)""")
        db.execute("CREATE TABLE product_master(product_id VARCHAR, category VARCHAR, brand_name VARCHAR)")
        db.execute("INSERT INTO product_master VALUES ('x','A','One'),('y','B','Two')")
        # The same order has several lines, and one customer's earlier order precedes the cohort window.
        db.execute("""INSERT INTO sales VALUES
            ('a','old','x','2024-12-01','Sale',10,10,'2024-12-03',NULL,NULL),
            ('a','a1','x','2025-01-01','Sale',10,10,'2025-01-03',NULL,NULL),
            ('a','a1','x','2025-01-01','Sale',20,20,'2025-01-03',NULL,NULL),
            ('a','a2','x','2025-01-04','Sale',40,40,'2025-01-06',5,NULL),
            ('b','b1','y','2025-01-02','Sale',50,50,NULL,NULL,'2025-01-03'),
            ('b','b2','y','2025-01-05','Sale',60,60,'2025-01-07',NULL,NULL),
            ('c','c1','x','2025-01-30','Sale',70,70,'2025-02-02',NULL,NULL),
            ('z','replacement','x','2025-01-04','Exchange',1000,1000,'2025-01-05',NULL,NULL)""")
        db.execute("ALTER TABLE sales ADD COLUMN is_first_order BOOLEAN DEFAULT FALSE")
        db.execute("UPDATE sales SET is_first_order = TRUE WHERE order_id IN ('old', 'b1', 'c1')")
        yield db


def spec(**changes):
    return QuerySpec.model_validate({"table": "sales", "measures": [{"name": "gmv", "metric": "gross_sales"}],
        "time": {"start": "2025-01-04", "end": "2025-01-05", "grain": "day"}, **changes})


@pytest.mark.parametrize("kind,values", [("rolling_sum", [120, 150]), ("running_sum", [40, 100])])
def test_windows_use_calendar_days_and_preserve_lookback(cat, sample, kind, values):
    query = spec(operation={"kind": kind, "days": 4})
    assert validate(query, cat) == []
    sql = compile_spec(query, cat)
    verdict = check_sql(sql)
    assert verdict.ok, verdict.errors
    frame = sample.execute(sql).df()
    assert frame[f"gmv_{kind}"].tolist() == values
    assert len(frame) == 2  # no manufactured days
    assert "observed days only" in readback(query, cat)


def test_periods_recompute_distinct_counts_and_denominators(cat, sample):
    query = spec(measures=[{"name": "orders", "metric": "orders"}, {"name": "aov", "metric": "aov"}],
                 time={"start": "2025-01-04", "end": "2025-01-05"},
                 operation={"kind": "period_compare", "previous_start": "2025-01-01", "previous_end": "2025-01-02"})
    assert not validate(query, cat)
    sql = compile_spec(query, cat)
    assert check_sql(sql).ok
    row = sample.execute(sql).df().iloc[0]
    assert row.orders == row.orders_previous == 2  # three lines in previous period, two orders
    assert row.aov == 50 and row.aov_previous == 30  # cancelled order excluded from AOV denominator
    assert row.aov_delta == 20 and row.aov_change_ratio == pytest.approx(2 / 3)
    assert canonical(query) != canonical(query.model_copy(update={"operation": None}))


def test_absent_comparison_groups_stay_unknown(cat, sample):
    query = spec(dimensions=["category"], time={"start": "2025-01-04", "end": "2025-01-04"},
                 operation={"kind": "period_compare", "previous_start": "2025-01-02", "previous_end": "2025-01-02"})
    frame = sample.execute(compile_spec(query, cat)).df().set_index("category")
    assert pd.isna(frame.loc['A', 'gmv_previous']) and pd.isna(frame.loc['B', 'gmv'])
    assert frame.gmv_delta.isna().all()


@pytest.mark.parametrize("metric", ["orders", "sessions", "return_rate", "aov"])
def test_nonadditive_windows_rejected(cat, metric):
    query = spec(table=cat.source_table(metric), measures=[{"name": "value", "metric": metric}],
                 operation={"kind": "rolling_sum"})
    assert any("time-additive" in p for p in validate(query, cat))


@pytest.mark.parametrize("change", [
    {"ranking": {"by": "gmv"}}, {"time": {"grain": "day"}},
    {"time": {"start": "2025-01-01", "end": "2027-01-01", "grain": "day"}},
    {"time": {"start": "2025-01-01", "end": "2025-01-05", "grain": "month"}},
    {"operation": {"kind": "period_compare"}},
])
def test_operation_scope_is_bounded(cat, change):
    assert validate(spec(**({"operation": {"kind": "rolling_sum"}} | change)), cat)


def test_cohort_maturity_and_first_ever_purchase(sample):
    sql, _ = build("cohort_retention", {"start": "2025-01-01", "end": "2025-01-31",
                                     "as_of": "2025-01-31", "days": 7})
    verdict = check_sql(sql)
    assert verdict.ok, verdict.errors
    row = sample.execute(sql).df().iloc[0]
    assert row.buyers == row.repeat_buyers == 1 and row.repeat_share == 1
    # 'a' belongs to December; 'c' is immature; only b is eligible and repeats.


def test_customer_segmentation_preaggregates_orders(sample):
    sql, _ = build("customer_segments", {"start": "2025-01-01", "end": "2025-01-05",
                                       "min_orders": 2, "recency_days": 2})
    verdict = check_sql(sql)
    assert verdict.ok, verdict.errors
    row = sample.execute(sql).df().iloc[0]
    assert row.segment == 'active_repeat' and row.customers == 2 and row.orders == 4
    assert row.realized_revenue == 125  # 30 + 35 + 0 + 60, replacement excluded


def test_funnel_order_duplicates_and_population(sample):
    sample.execute("CREATE TABLE clickstream(session_id VARCHAR, event_at TIMESTAMP, event_type VARCHAR)")
    sample.execute("""INSERT INTO clickstream VALUES
        ('a','2025-01-01 01:00','pdp_view'), ('a','2025-01-01 02:00','add_to_cart'),
        ('a','2025-01-01 03:00','purchase'), ('a','2025-01-01 04:00','purchase'),
        ('b','2025-01-01 01:00','purchase'), ('b','2025-01-01 02:00','pdp_view'),
        ('c','2025-01-01 01:00','pdp_view'), ('c','2025-01-01 01:00','add_to_cart'),
        ('d','2024-12-31','pdp_view')""")
    sql, _ = build("session_funnel", {"start": "2025-01-01", "end": "2025-01-02",
                                    "steps": ["pdp_view", "add_to_cart", "purchase"]})
    verdict = check_sql(sql)
    assert verdict.ok, verdict.errors
    frame = sample.execute(sql).df()
    assert frame.sessions.tolist() == [3, 1, 1]
    assert frame.conversion.tolist() == pytest.approx([1, 1 / 3, 1 / 3])


@pytest.mark.parametrize("params", [
    {"steps": ["purchase", "purchase"]}, {"steps": ["purchase", "DROP TABLE sales"]},
    {"steps": ["pdp_view", "purchase"], "end": "2027-01-01"},
])
def test_funnel_rejects_unknown_events_unbounded_dates_and_repeated_steps(params):
    with pytest.raises(TemplateError):
        build("session_funnel", {"start": "2025-01-01", "end": "2025-01-02", **params})


def test_standing_instructions_block_fast_path_but_old_questions_do_not(cat):
    assert independent_history("User: Show revenue by category.\nAnalyst: some result", cat, "2025-01-05")
    assert not independent_history("User: Always exclude cancelled orders", cat, "2025-01-05")
    assert not independent_history("User: Show revenue\nUser: Only for Delhi", cat, "2025-01-05")
    assert not independent_history("unstructured context", cat, "2025-01-05")


def test_followup_keeps_date_metric_and_population(cat):
    base = parse("Show revenue by category for yesterday", cat, "2025-01-05")
    changed = followup("And by brand?", base, cat, "2025-01-05")
    assert changed.dimensions == ["brand"] and changed.time == base.time and changed.measures == base.measures
    changed = followup("same for today", base, cat, "2025-01-05")
    assert changed.time.start == date(2025, 1, 5) and changed.dimensions == base.dimensions
    assert followup("same but excluding returns", base, cat, "2025-01-05") is None


def test_intent_dates_grain_and_entity_are_not_silently_dropped(cat):
    base = QuerySpec(table="sales", measures=[Measure(name="gmv", metric="gross_sales")])
    assert intent_problems("GMV for yesterday", base, cat, "2025-01-05")
    assert intent_problems("Monthly GMV", base, cat)
    assert intent_problems("GMV from 2025-01-01 to 2025-01-04", base, cat)
    assert intent_problems("GMV for Mumbai", base, cat)
    assert parse("Marketing spend by channel", cat, "2025-01-05").dimensions == ["marketing_channel"]


@pytest.mark.parametrize("sentence,op,value,okay", [
    ("Revenue increased by {c1}", "diff", -10, False),
    ("Revenue fell by {c1}", "diff", -10, True),
    ("Revenue increased to {c1}", "value", 20, False),
    ("Revenue is highest at {c1}", "value", 20, False),
    ("Revenue is attributable to ads: {c1}", "value", 20, False),
])
def test_qualitative_claims_need_matching_evidence(sentence, op, value, okay):
    frame = pd.DataFrame({"period": ["now", "before"], "revenue": [20, 30]})
    claim = Claim(id="c1", value=value, unit="inr", column="revenue", op=op,
                  row=[KeyValue(column="period", value="now")],
                  against=[KeyValue(column="period", value="before")])
    assert verify(frame, [claim], {"headline": sentence}, units={"revenue": "inr"}).ok == okay


def test_entity_filter_has_unique_stored_identity(cat):
    query = parse('Show revenue where city is "mumbai"', cat, '2025-01-05')
    assert query.filters[0].values == ['Mumbai']
    assert parse('Show revenue where city is Atlantis', cat, '2025-01-05') is None
    assert parse('Show revenue where city is Mumbai excluding returns', cat, '2025-01-05') is None


def test_clarifying_period_does_not_disable_original_metric_validation(cat):
    from pantree_insights.interpret import decide
    from pantree_insights.llm import FakeLLM
    wrong = {'route': 'spec', 'restated_question': 'Net sales', 'spec': {
        'table': 'sales', 'measures': [{'name': 'net_sales', 'metric': 'net_sales'}]}}
    result = decide(FakeLLM(lambda *_: wrong), cat, {}, {},
                    {'first_day': '2025-01-01', 'last_day': '2025-01-05'}, 'What is GMV?',
                    answers={'Which period?': 'All data'}, contrast=False)
    assert result.kind == 'failed'
    assert any('explicitly requests gross_sales' in p for p in result.detail['problems'])


def test_shared_history_and_memory_still_use_zero_models(con, tmp_path):
    from pantree_insights.graph import Agent
    from pantree_insights.llm import FakeLLM
    from pantree_insights.memory import Memory
    def forbidden(*_):
        raise AssertionError('unexpected model call')
    memory = Memory(tmp_path, 'small')
    agent = Agent(llm_factory=lambda: FakeLLM(forbidden), con=con, memory=memory)
    try:
        run_id, events = agent.start('Show revenue by category')
        proposal = list(events)[-1]
        first = list(agent.resume(run_id, 'Go ahead', proposal['plan_version']))[-1]
        assert first['status'] == 'done'
        run_id, events = agent.start('And by brand?', history='User: Show revenue by category',
                                     previous_spec=first['spec'])
        proposal = list(events)[-1]
        final = list(agent.resume(run_id, 'Go ahead', proposal['plan_version']))[-1]
        assert final['status'] == 'done' and final['spec']['dimensions'] == ['brand'] and final['cost']['calls'] == 0
    finally:
        agent.close()


def test_multifact_period_comparison_reconciles_both_native_populations(cat, con):
    query = QuerySpec.model_validate({'table': 'marketing_attribution', 'measures': [
        {'name': 'revenue', 'metric': 'last_click_revenue'}, {'name': 'spend', 'metric': 'marketing_spend'},
        {'name': 'roas', 'ratio': ['revenue', 'spend']}],
        'time': {'start': '2026-01-01', 'end': '2026-01-31'},
        'operation': {'kind': 'period_compare', 'previous_start': '2025-12-01', 'previous_end': '2025-12-31'}})
    assert not validate(query, cat)
    sql = compile_spec(query, cat)
    assert check_sql(sql).ok
    result = con.execute(sql).df().iloc[0]
    for suffix, start, end in [('', '2026-01-01', '2026-02-01'), ('_previous', '2025-12-01', '2026-01-01')]:
        revenue = con.execute('''SELECT SUM(credit_last_click * order_revenue) FROM marketing_attribution
            WHERE campaign_id IS NOT NULL AND order_at >= ? AND order_at < ?''', [start, end]).fetchone()[0]
        spend = con.execute('''SELECT SUM(spend) FROM marketing_spend
            WHERE date_hour >= ? AND date_hour < ?''', [start, end]).fetchone()[0]
        assert result['revenue' + suffix] == pytest.approx(revenue)
        assert result['spend' + suffix] == pytest.approx(spend)
        assert result['roas' + suffix] == pytest.approx(revenue / spend)


def test_semantic_policy_rejects_additive_ratio_and_distinct_counts():
    from pydantic import ValidationError

    from pantree_insights.semantic import Metric
    for numerator, denominator in [('COUNT(DISTINCT s.order_id)', None), ('SUM(s.net_amount)', 'COUNT(*)')]:
        with pytest.raises(ValidationError, match='cannot be declared time-additive'):
            Metric(label='bad', description='bad', source='sales', numerator=numerator, denominator=denominator,
                   unit='inr', time_aggregation='additive')


def test_contributions_reconcile_to_net_change(cat, sample):
    query = spec(dimensions=['category'], time={'start': '2025-01-04', 'end': '2025-01-05'}, shares=['gmv'],
                 operation={'kind': 'period_compare', 'previous_start': '2025-01-01', 'previous_end': '2025-01-02'})
    frame = sample.execute(compile_spec(query, cat)).df().set_index('category')
    # A moves 30 -> 40; B moves 50 -> 60. Each contributes half of the net +20.
    assert frame.gmv_delta.to_dict() == {'A': 10, 'B': 10}
    assert frame.gmv_contribution.to_dict() == {'A': .5, 'B': .5}


@pytest.mark.parametrize('metric', ['orders', 'aov', 'return_rate'])
def test_nonadditive_share_rejected(cat, metric):
    assert any('additive' in p for p in validate(spec(measures=[{'name': 'value', 'metric': metric}],
                                                      shares=['value']), cat))


def test_entity_attribution_is_not_just_numeric_equality():
    frame = pd.DataFrame({'city': ['Delhi', 'Mumbai'], 'revenue': [100, 100]})
    claim = Claim(id='c1', column='revenue', row=[KeyValue(column='city', value='Delhi')], value=100, unit='inr')
    assert verify(frame, [claim], {'headline': 'Delhi revenue was {c1}.'}).ok
    assert not verify(frame, [claim], {'headline': 'Mumbai revenue was {c1}.'}).ok


@pytest.mark.parametrize('changes,reason', [
    ({'time': {'start': '2025-01-01', 'end': '2025-01-02'},
      'operation': {'kind': 'rolling_sum', 'days': 3}}, 'lookback'),
    ({'operation': {'kind': 'period_compare', 'previous_start': '2024-12-30',
                    'previous_end': '2024-12-31'}}, 'comparison'),
    ({'operation': {'kind': 'period_compare', 'previous_start': '2025-01-01',
                    'previous_end': '2025-01-03'}}, 'equal-duration'),
])
def test_coverage_includes_lookback_and_comparison(changes, reason):
    from pantree_insights.interpret import coverage_problems
    assert reason in ' '.join(coverage_problems(spec(**changes), None,
                                              {'first_day': '2025-01-01', 'last_day': '2025-01-31'}))


def test_segmented_funnel_keeps_entry_population_and_zero_dropoff():
    with duckdb.connect() as db:
        db.execute('CREATE TABLE clickstream(session_id VARCHAR,event_at TIMESTAMP,event_type VARCHAR,device VARCHAR)')
        db.execute("""INSERT INTO clickstream VALUES
          ('a','2025-01-01 10:00','pdp_view','mobile'),
          ('a','2025-01-01 10:01','add_to_cart','desktop'),
          ('a','2025-01-01 10:02','purchase','desktop'),
          ('b','2025-01-01 11:00','pdp_view','desktop'),
          ('c','2025-01-01 12:00','purchase','mobile'),
          ('c','2025-01-01 12:01','pdp_view','mobile')""")
        sql, reading = build('session_funnel', {'start': '2025-01-01', 'end': '2025-01-01',
            'steps': ['pdp_view', 'add_to_cart', 'purchase'], 'by': 'device'})
        assert check_sql(sql).ok
        frame = db.execute(sql).df().set_index(['device', 'step'])
        assert frame.loc[('mobile', 'pdp_view'), 'sessions'] == 2
        assert frame.loc[('mobile', 'purchase'), 'sessions'] == 1
        assert frame.loc[('desktop', 'purchase'), 'sessions'] == 0
        assert frame.loc[('mobile', 'purchase'), 'conversion'] == .5
        assert 'first step' in reading


def test_template_cannot_silently_drop_requested_population(cat):
    from pantree_insights.interpret import TemplateCall, template_intent_problems
    data = {'first_day': '2025-01-01', 'last_day': '2026-11-16'}
    call = TemplateCall(name='customer_segments', start='2025-01-01', end='2025-01-31')
    assert any('restricts city' in p for p in template_intent_problems('Customer segments in Mumbai', call, cat, data))
    funnel = TemplateCall(name='session_funnel', start='2025-01-01', end='2025-01-31')
    assert any('split by device' in p for p in template_intent_problems('Funnel by device', funnel, cat, data))
    assert any('gross_sales' in p for p in template_intent_problems('Funnel and GMV', funnel, cat, data))



def test_two_true_values_cannot_swap_entities_in_one_sentence():
    frame = pd.DataFrame({'city': ['Delhi', 'Mumbai'], 'revenue': [100, 200]})
    claims = [Claim(id='c1', column='revenue', row=[KeyValue(column='city', value='Delhi')], value=100, unit='inr'),
              Claim(id='c2', column='revenue', row=[KeyValue(column='city', value='Mumbai')], value=200, unit='inr')]
    assert verify(frame, claims, {'headline': 'Delhi {c1}, Mumbai {c2}.'}).ok
    assert not verify(frame, claims, {'headline': 'Mumbai {c1}, Delhi {c2}.'}).ok
