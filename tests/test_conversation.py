"""The confirmation gate is a server invariant, including direct node entry and plan revisions."""
import copy

import pytest

from pantree_insights.graph import Agent
from pantree_insights.llm import FakeLLM
from pantree_insights.memory import Memory


@pytest.fixture
def agent(con, tmp_path):
    def no_model(*_):
        raise AssertionError('This deterministic conversation should not call a model')
    item = Agent(con=con, memory=Memory(tmp_path, 'small'), llm_factory=lambda: FakeLLM(no_model))
    yield item
    item.close()


def pending(agent, question='Show revenue by category'):
    run_id, events = agent.start(question, use_defaults=True)
    events = list(events)
    assert events[-1]['kind'] == 'confirm'
    assert not any(e['type'] == 'result' or e.get('step') == 'execute' for e in events)
    return run_id, events[-1]


@pytest.mark.parametrize('text', ['Yes', 'Proceed', 'Looks correct', 'Go ahead', 'Yes, please'])
def test_every_fastpath_requires_explicit_current_plan_confirmation(agent, text, monkeypatch):
    calls = []
    query = agent.query
    monkeypatch.setattr(agent, 'query', lambda sql: calls.append(sql) or query(sql))
    run_id, plan = pending(agent)
    assert calls == []
    final = list(agent.resume(run_id, text, plan['plan_version']))[-1]
    assert final['status'] == 'done' and len(calls) == 1


@pytest.mark.parametrize('reply,version', [('', 1), (None, 1), ({}, 1), ('Yes', None), ('Yes', 0)])
def test_empty_missing_and_stale_replies_never_execute(agent, reply, version, monkeypatch):
    run_id, _ = pending(agent)
    monkeypatch.setattr(agent, 'query', lambda *_: pytest.fail('SQL before valid confirmation'))
    events = list(agent.resume(run_id, reply, version))
    assert not any(e['type'] == 'result' for e in events)


def test_revision_preserves_metric_period_and_requires_new_version(agent):
    run_id, first = pending(agent, 'Show revenue by category for yesterday')
    second = list(agent.resume(run_id, 'No, by brand instead', first['plan_version']))[-1]
    assert second['kind'] == 'confirm' and second['plan_version'] > first['plan_version']
    assert second['spec']['dimensions'] == ['brand']
    assert second['spec']['time'] == first['spec']['time']
    assert second['spec']['measures'] == first['spec']['measures']
    stale = list(agent.resume(run_id, 'Yes', first['plan_version']))[-1]
    assert stale['kind'] == 'confirm'
    final = list(agent.resume(run_id, 'Go ahead', second['plan_version']))[-1]
    assert final['status'] == 'done' and final['spec'] == second['spec']


def test_direct_execution_boundary_rejects_unconfirmed_and_tampered_state(agent, monkeypatch):
    run_id, _ = pending(agent)
    state = agent.graph.get_state({'configurable': {'thread_id': run_id}}).values
    monkeypatch.setattr(agent, 'query', lambda *_: pytest.fail('bypassed approval boundary'))
    assert agent.execute(state)['status'] == 'failed'
    state = copy.deepcopy(state)
    state['confirmed_digest'] = state['plan_digest']
    state['prepared'][0]['sql'] = 'SELECT COUNT(*) FROM customers'
    assert agent.execute(state)['status'] == 'failed'


def test_cancellation_finishes_without_query(agent, monkeypatch):
    run_id, plan = pending(agent)
    monkeypatch.setattr(agent, 'query', lambda *_: pytest.fail('cancelled SQL'))
    assert list(agent.resume(run_id, 'Cancel', plan['plan_version']))[-1]['status'] == 'cancelled'


def test_one_confirmation_executes_entire_bounded_plan(agent, monkeypatch):
    from pantree_insights.interpret import AnalysisStep, Decision
    from pantree_insights.spec import Measure, QuerySpec
    steps = [AnalysisStep(purpose=metric, spec=QuerySpec(table='sales', measures=[Measure(name=metric, metric=metric)]))
             for metric in ['gross_sales', 'orders']]
    monkeypatch.setattr('pantree_insights.graph.nodes_interpret.decide', lambda *a, **kw:
                        Decision('investigation', steps=steps, restated='investigate sales volume and value'))
    calls = []
    query = agent.query
    monkeypatch.setattr(agent, 'query', lambda sql: calls.append(sql) or query(sql))
    run_id, events = agent.start('Investigate sales volume and value')
    proposal = list(events)[-1]
    assert len(proposal['plan']) == 2 and calls == []
    final = list(agent.resume(run_id, 'Proceed', proposal['plan_version']))[-1]
    assert final['status'] == 'done' and len(calls) == len(final['analyses']) == 2
    assert calls == [item['sql'] for item in proposal['plan']]
    assert all(item['grounded'] and 'chart' in item for item in final['analyses'])
    with pytest.raises(ValueError, match='no pending'):
        list(agent.resume(run_id, 'Proceed', proposal['plan_version']))
    assert len(calls) == 2


def test_batch_revision_requires_target_and_preserves_other_steps(agent, monkeypatch):
    from pantree_insights.interpret import AnalysisStep, Decision
    from pantree_insights.spec import Measure, QuerySpec
    steps = [AnalysisStep(purpose=metric, spec=QuerySpec(table='sales', measures=[Measure(name=metric, metric=metric)]))
             for metric in ['gross_sales', 'orders']]
    monkeypatch.setattr('pantree_insights.graph.nodes_interpret.decide', lambda *a, **kw:
                        Decision('investigation', steps=steps, restated='investigate sales'))
    run_id, events = agent.start('Investigate sales')
    first = list(events)[-1]
    unclear = list(agent.resume(run_id, 'By brand instead', first['plan_version']))[-1]
    assert unclear['kind'] == 'clarify'
    revised = list(agent.resume(run_id, 'step 1: by brand', unclear['plan_version']))[-1]
    assert revised['kind'] == 'confirm' and revised['plan_version'] > first['plan_version']
    assert revised['plan'][0]['spec']['dimensions'] == ['brand']
    assert revised['plan'][1] == first['plan'][1]


def test_structured_patch_retains_untouched_requirements(agent):
    from pantree_insights.interpret import revise
    from pantree_insights.spec import QuerySpec
    original = QuerySpec.model_validate({'table': 'sales', 'dimensions': ['category'],
        'measures': [{'name': 'revenue', 'metric': 'realized_revenue'}],
        'filters': [{'field': 'payment_method', 'values': ['COD']}],
        'time': {'start': '2026-01-01', 'end': '2026-01-31'}})
    model = FakeLLM(lambda *_: {'dimensions': ['brand']})
    changed = revise(model, agent.catalog, original, 'Group the same population using brands', agent.data).spec
    assert changed.dimensions == ['brand']
    assert changed.model_dump(exclude={'dimensions'}) == original.model_dump(exclude={'dimensions'})


def test_saved_evidence_enters_interpreter_beyond_recent_window(agent, monkeypatch):
    captured = []
    from pantree_insights.interpret import Decision
    def decide(*a, **kw):
        captured.append(kw['background'])
        return Decision('unanswerable', reason='Needs evidence')
    monkeypatch.setattr('pantree_insights.graph.nodes_interpret.decide', decide)
    context = {'objective': 'Investigate declining volume', 'origin_plan': [{'metric': 'orders'}],
               'evidence': [{'finding': 'Earlier verified result'}]}
    _, events = agent.start('What should we investigate next?', history='', investigation=context)
    list(events)
    assert 'Earlier verified result' in captured[0] and 'Investigate declining volume' in captured[0]



def test_explicit_followup_uses_saved_plan_without_model(agent):
    run_id, proposal = pending(agent)
    final = list(agent.resume(run_id, 'Go ahead', proposal['plan_version']))[-1]
    run_id, stream = agent.start('And by brand?', previous_spec=final['spec'],
        history='User: Show revenue by category', investigation={'objective': 'Find value drivers',
        'current_plan': final['confirmed_plan']})
    proposed = list(stream)[-1]
    assert proposed['kind'] == 'confirm' and proposed['spec']['dimensions'] == ['brand']
    final = list(agent.resume(run_id, 'Go ahead', proposed['plan_version']))[-1]
    assert final['cost']['calls'] == 0 and final['objective'] == 'Find value drivers'


def test_interpreter_proposes_dynamic_multiquery_scope(agent, monkeypatch):
    model = FakeLLM(lambda *_: {'route': 'investigation', 'relationship': 'investigation',
        'restated_question': 'Investigate gross value and order volume', 'steps': [
            {'purpose': 'Quantify value', 'spec': {'table': 'sales',
                'measures': [{'name': 'gmv', 'metric': 'gross_sales'}]}},
            {'purpose': 'Quantify volume', 'spec': {'table': 'sales',
                'measures': [{'name': 'orders', 'metric': 'orders'}]}}
        ]})
    monkeypatch.setattr(agent, 'llm_factory', lambda: model)
    run_id, stream = agent.start('Investigate GMV and orders')
    proposal = list(stream)[-1]
    assert proposal['kind'] == 'confirm' and len(proposal['plan']) == 2
    assert [item['purpose'] for item in proposal['plan']] == ['Quantify value', 'Quantify volume']
    final = list(agent.resume(run_id, 'Looks correct', proposal['plan_version']))[-1]
    assert final['status'] == 'done' and len(final['analyses']) == 2 and final['cost']['calls'] == 1
