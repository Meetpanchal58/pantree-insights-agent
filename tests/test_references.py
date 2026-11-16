"""Reference queries picked by structure: same tables, columns and metrics as the understood question."""
from pantree_insights.references import ReferenceIndex
from pantree_insights.semantic import load_examples, load_library

INDEX = ReferenceIndex(load_examples().examples + load_library())


def test_picks_queries_with_the_same_columns_and_metrics_whatever_the_wording():
    # "how often do parcels bounce back, per courier" shares no wording with "RTO rate by courier"
    picked = INDEX.pick(tables={"sales"}, columns={"sales.courier", "sales.rto_at", "sales.shipped_at"},
                        metrics={"rto_rate"})
    assert picked[0].id == "lib-del-02"


def test_hidden_examples_are_never_picked():
    """Evaluation hides each question's own example; structural picking must respect that too."""
    columns = {"sales.courier", "sales.rto_at", "sales.shipped_at"}
    picked = INDEX.pick(tables={"sales"}, columns=columns, metrics={"rto_rate"}, exclude={"lib-del-02"})
    assert "lib-del-02" not in [e.id for e in picked]


def test_needs_a_shared_table_and_prefers_fewer_unrelated_tables():
    picked = INDEX.pick(tables={"support_contacts", "agents"}, columns={"agents.team"}, metrics={"avg_csat"})
    assert picked and all("support_contacts" in INDEX.usage[e.id].tables for e in picked)
    assert picked[0].id == "lib-sup-03"
    assert INDEX.pick(tables={"no_such_table"}, columns=set(), metrics=set()) == []


def test_render_shows_question_and_sql():
    text = INDEX.render(INDEX.pick(tables={"sales"}, columns={"sales.courier"}, metrics={"rto_rate"}, k=1))
    assert text.startswith("-- ") and "SELECT" in text
