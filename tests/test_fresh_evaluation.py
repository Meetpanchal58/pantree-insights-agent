"""Free checks of selection and the evaluation-only spending boundary."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from run_fresh_eval import EvaluationStore, sample  # noqa: E402

from pantree_insights.config import get_settings  # noqa: E402
from pantree_insights.llm import BudgetExceeded  # noqa: E402


def test_sample_is_reproducible_unique_and_covers_strata():
    items = [{'suite': 's', 'set': group, 'id': str(i)} for group, n in [('a',90),('b',9),('c',1)] for i in range(n)]
    selected = sample(items, 20, 42)
    assert selected == sample(list(reversed(items)), 20, 42)
    assert len({(i['set'],i['id']) for i in selected}) == 20
    assert {i['set'] for i in selected} == {'a','b','c'}
    assert selected != sample(items, 20, 43)


def test_budget_keeps_unknown_calls_reserved_and_counts_all_days(tmp_path, monkeypatch):
    from sqlalchemy import update

    from pantree_insights.store import api_calls

    settings = get_settings()
    monkeypatch.setattr(settings, 'daily_budget_usd', 4)
    monkeypatch.setattr(settings, 'daily_user_budget_usd', 4)
    store = EvaluationStore(f'sqlite:///{tmp_path}/budget.db', 4)
    user = store.upsert_user('test@example.test')
    pin = {'user_id':user['id'],'billing':'shared','provider':'openai','model':'test'}
    try:
        first = store.reserve_api_call(pin, 'r1', 'interpreter', 2)
        store.settle_api_call(first, .5, {})
        second = store.reserve_api_call(pin, 'r2', 'analyst', 3)
        store.settle_api_call(second, None, {}, 'uncertain')
        store._exec(update(api_calls).values(day='2000-01-01'))
        with pytest.raises(BudgetExceeded):
            store.reserve_api_call(pin,'r3','analyst',.51)
        assert store.stopped
        assert store.totals() == {'known_usd':.5,'held_usd':3,'calls':2,'uncertain_calls':1}
    finally:
        store.engine.dispose()
