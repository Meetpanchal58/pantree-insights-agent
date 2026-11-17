"""The knowledge base: every table and column explained, and the catalog in sync with the schema and the data rules."""
import pytest

from pantree_insights.catalog import MAX_VALUES, describe, load_catalog, value_columns
from pantree_insights.semantic import load_schema

SCHEMA, CATALOG = load_schema(), load_catalog()
COLUMNS = [(t, c, d) for t, info in SCHEMA.tables.items() for c, d in info.columns.items()]


@pytest.mark.parametrize(("table", "column", "desc"), COLUMNS, ids=[f"{t}.{c}" for t, c, _ in COLUMNS])
def test_every_column_has_a_meaning(table, column, desc):
    kind, _, meaning = desc.partition(" - ")
    assert kind in ("text", "number", "integer", "timestamp", "boolean") and len(meaning.strip()) >= 3, desc


def test_every_table_says_what_a_row_is():
    for t, info in SCHEMA.tables.items():
        assert info.entity and info.grain and len(info.description) >= 60, t


def test_catalog_matches_the_schema():
    assert set(CATALOG) == set(SCHEMA.tables)
    for t, info in SCHEMA.tables.items():
        assert set(CATALOG[t]["columns"]) == set(info.columns), f"{t}: run pantree-catalog"
        for c, d in info.columns.items():
            assert CATALOG[t]["columns"][c]["meaning"] == d.partition(" - ")[2], f"{t}.{c}: run pantree-catalog"


def test_values_only_for_small_categories_structure_for_the_rest():
    for t, info in CATALOG.items():
        for c, e in info["columns"].items():
            assert e["role"] in ("id", "category", "text", "measure", "date", "flag"), (t, c)
            if e["role"] == "category":
                assert 0 < len(e["values"]) <= MAX_VALUES, (t, c)
            else:
                assert "values" not in e, f"{t}.{c} stores values but is {e['role']}"
            if e["role"] == "text":
                assert e.get("structure"), (t, c)
            if e["distinct"] > MAX_VALUES:
                assert "values" not in e, f"{t}.{c} has {e['distinct']} distinct values: keep only its structure"


def test_identifiers_and_codes_carry_a_format_not_values():
    assert CATALOG["sales"]["columns"]["order_id"]["format"] == "OD\\d{9}"
    assert CATALOG["sales"]["columns"]["ship_pincode"]["format"] == "\\d{6}"
    assert "values" not in CATALOG["product_master"]["columns"]["product_name"]


def test_shared_column_names_are_told_apart_by_table():
    rating = CATALOG["sellers"]["columns"]["rating"]
    assert "seller" in rating["meaning"] and any("product_reviews" in o for o in rating["also_in"])
    text = describe(["customers"])
    assert "city where the customer signed up" in text and "Same name in" in text


def test_prompt_lists_values_and_skips_text_not_for_analysis():
    text = describe(["sales", "product_master"])
    assert "COD" in text and "'Bengaluru'" in text and "Format: OD\\d{9}" in text      # meaning or value list
    assert "image_url" not in text and "description (text)" not in text


def test_value_lookup_knows_where_each_value_lives():
    where = value_columns()
    assert "sales.payment_method" in where["COD"]
    assert {"customers.city", "sellers.city", "warehouses.city"} <= set(where["Mumbai"])
