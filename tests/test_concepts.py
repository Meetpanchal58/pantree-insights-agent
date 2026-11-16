"""concepts.yaml: subjects and concepts point to real columns, reachable without multiplying rows."""
import sys
from pathlib import Path

import pytest

from pantree_insights.joins import JoinGraph
from pantree_insights.semantic import load_concepts, load_schema

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_joins import fake_profile  # noqa: E402

SCHEMA, CONCEPTS = load_schema(), load_concepts()
GRAPH = JoinGraph(SCHEMA, fake_profile())


def exists(qualified: str) -> bool:
    table, column = qualified.split(".", 1)
    return table in SCHEMA.tables and column in SCHEMA.tables[table].columns


@pytest.mark.parametrize("name", sorted(CONCEPTS.subjects))
def test_subject_tables_and_time_columns_exist(name):
    subject = CONCEPTS.subjects[name]
    assert subject.table in SCHEMA.tables
    for column in [subject.time, *subject.time_meanings.values()]:
        assert exists(column) and column.startswith(subject.table + "."), column


ENTRIES = [(c, s, e.column) for c, entries in CONCEPTS.concepts.items() for s, e in entries.items()]


@pytest.mark.parametrize(("concept", "subject", "column"), ENTRIES)
def test_concept_columns_exist_and_join_without_multiplying_rows(concept, subject, column):
    assert exists(column), column
    root, table = CONCEPTS.subjects[subject].table, column.split(".")[0]
    path = GRAPH.path(root, [table])
    assert not any(step.fans_out for step in path), f"{concept}/{subject}: {[s.join for s in path]}"


def test_same_word_different_column_by_subject():
    assert CONCEPTS.column("city", "orders").column == "sales.ship_city"
    assert CONCEPTS.column("city", "signups").column == "customers.city"
    assert CONCEPTS.column("city", "ad_spend") is None                # that cut does not exist
    assert CONCEPTS.column("channel_group", "attribution").column == "marketing_attribution.channel_group"


def test_columns_map_back_to_concepts():
    assert CONCEPTS.concepts_of_column("customers.city") == {"city"}
    assert CONCEPTS.concepts_of_column("sellers.city") == {"seller_city"}
    assert CONCEPTS.concepts_of_column("warehouses.city") == {"warehouse"}
