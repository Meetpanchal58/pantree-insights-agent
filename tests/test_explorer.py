"""Real bounded previews and governance metadata; browsing never executes analytical plans."""

import pytest

from pantree_insights import explorer
from pantree_insights.semantic import load_schema

pytestmark = pytest.mark.data


def test_overview_contains_every_governed_table_and_only_declared_relationships(con):
    result = explorer.overview()
    schema = load_schema()
    assert {t["name"] for t in result["tables"]} == set(schema.tables)
    assert len(result["relationships"]) == len(schema.joins)
    for relation in result["relationships"]:
        assert f"{relation['left']} = {relation['right']}" in schema.joins
        assert relation["cardinality"] in ("1:1", "N:1", "1:N", "N:N", "unverified")
    assert "customer_pii" not in str(result)
    assert not any(c["name"] == "address_line" for t in result["tables"] for c in t["columns"])


@pytest.mark.parametrize("table", ["product_master", "sales", "auth_events"])
def test_actual_samples_types_counts_and_masking(con, table):
    result = explorer.preview(table)
    assert len(result["rows"]) <= 30
    assert result["row_count"] == con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    assert result["column_count"] == len(result["dictionary"])
    for c in result["dictionary"]:
        assert c["type"]
        if c["masked"]:
            assert c["values"] == [] and all(row[c["name"]] is None for row in result["rows"])
        else:
            actual = con.execute(f'SELECT "{c["name"]}" FROM {table} LIMIT 1').fetchone()[0]
            if c["type"] == "VARCHAR" and actual is not None:
                assert result["rows"][0][c["name"]] == actual
    for c in result["dictionary"]:
        if c["name"].endswith("_id") and not c["masked"]:
            assert len(c["values"]) <= 3 and not c["complete"]


def test_only_proven_complete_value_sets_are_labeled_complete(con):
    data = explorer.preview("warehouses")
    for c in data["dictionary"]:
        if c["complete"]:
            actual = con.execute(f'SELECT DISTINCT "{c["name"]}" FROM warehouses WHERE "{c["name"]}" IS NOT NULL')
            assert set(c["values"]) == {row[0] for row in actual.fetchall()}
    sales = explorer.preview("sales")
    assert any(c["more_values"] and not c["complete"] for c in sales["dictionary"])
    assert all(c["range_scope"] == "sample" for c in sales["dictionary"] if c["range"])


@pytest.mark.parametrize("name", ["customer_pii", "users", "api_calls", "sales; DROP TABLE users", "../sales"])
def test_visibility_and_injection_rejected(name):
    with pytest.raises(KeyError):
        explorer.preview(name)


def test_preview_requires_current_snapshot_and_is_cached():
    with pytest.raises(ValueError, match="changed"):
        explorer.preview("sales", "old-snapshot")
    before = explorer._preview.cache_info().hits
    explorer.preview("sales")
    explorer.preview("sales")
    assert explorer._preview.cache_info().hits > before
