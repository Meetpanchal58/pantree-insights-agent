"""The daily LLM budget is stored in the database, so a restart does not reset it."""
from pantree_insights import store
from pantree_insights.llm import BUDGET_KEY


def test_spend_survives_a_new_store_instance(tmp_path):
    url = f"sqlite:///{tmp_path / 'app.db'}"
    store.Store(url).add_usd_today(BUDGET_KEY, 0.12)
    store.Store(url).add_usd_today(BUDGET_KEY, 0.03)
    assert abs(store.Store(url).usd_today(BUDGET_KEY) - 0.15) < 1e-9     # a fresh instance sees it
    assert store.Store(url).usd_today("someone-else") == 0.0
