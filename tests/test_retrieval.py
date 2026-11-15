"""Retrieval quality on the small shop (downloads a 65 MB embedding model on first run)."""
import pytest

from pantree_insights.semantic import load_examples, load_schema, tables_in

pytestmark = pytest.mark.data


@pytest.fixture(scope="module")
def retriever(con):
    from pantree_insights.retrieval import Retriever

    return Retriever(con)


def test_indexes_built(retriever):
    assert retriever.sizes["tables"] == 22
    assert retriever.sizes["examples"] == len(load_examples().examples)
    assert retriever.sizes["values"] > 1000


def test_tables_found_for_held_out_examples(retriever):
    """Leave-one-out: hide each example, ask its question, check every table its SQL uses is retrieved."""
    allowed = set(load_schema().tables)
    examples = load_examples().examples
    hits = 0
    for e in examples:
        context = retriever.retrieve(e.question, exclude_examples={e.id})
        hits += (tables_in(e.sql) & allowed) <= set(context.tables)
    assert hits / len(examples) >= 0.9, f"table recall {hits}/{len(examples)}"


@pytest.mark.parametrize("question,value,column", [
    ("How did Diwali 2026 do?", "Diwali 2026", "sale_events.name"),
    ("RTO for cash on delivery orders", "COD", "sales.payment_method"),
    ("Are footwear returns higher than apparel?", "Footwear", "product_master.category"),
    ("Is Meta advertising worth it?", "Meta", "campaigns.channel"),
    ("How late is Delhivery?", "Delhivery", "sales.courier"),
    ("returns because of size issues", "Size Issue", "sales.return_reason"),
])
def test_filter_values_found(retriever, question, value, column):
    context = retriever.retrieve(question)
    assert any(v.value == value and column in v.columns for v in context.values), \
        [(v.value, v.columns) for v in context.values]


@pytest.mark.parametrize("question,metric", [
    ("What is our revenue after refunds?", "realized_revenue"),
    ("money we actually keep after returns", "realized_revenue"),
    ("How often do parcels come back undelivered?", "rto_rate"),
    ("What share of visits buy something?", "conversion_rate"),
    ("Are we answering customers quickly?", "avg_first_response_minutes"),
    ("How much does it cost to get a new customer?", "customer_acquisition_cost"),
])
def test_metrics_found(retriever, question, metric):
    assert metric in retriever.retrieve(question).metrics


def test_prompt_has_definitions_values_and_joins(retriever):
    context = retriever.retrieve("Return rate by category for footwear vs apparel")
    prompt = context.to_prompt()
    assert "## Tables" in prompt and "TABLE sales" in prompt
    assert "return_requested_at" in prompt                 # metric SQL is included
    assert "'Footwear'" in prompt
    for join in context.joins:                             # joins only between retrieved tables
        assert all(side.strip().split(".")[0] in context.tables for side in join.split("="))


def test_glossary_aliases_point_to_real_values(retriever):
    from pantree_insights.semantic import load_glossary

    missing = [value for value in load_glossary().value_aliases if value not in retriever.values]
    assert not missing, f"glossary values not found in the data: {missing}"


def test_terms_added_to_context(retriever):
    context = retriever.retrieve("What is our RTO rate for COD vs prepaid?")
    assert {"RTO", "COD", "prepaid"} <= set(context.terms)
    assert "Business terms" in context.to_prompt()


def test_held_out_example_is_never_retrieved(retriever):
    """Evaluation hides each question's own example; it must not come back through retrieval."""
    for e in load_examples().examples:
        context = retriever.retrieve(e.question, exclude_examples={e.id})
        assert e.id not in {x.id for x in context.examples}


@pytest.mark.parametrize("question", [
    "Which delivery regions have the most cancellations?",
    "How does the return rate differ by the state orders are shipped to?",
    "Compare AOV across city tiers of the delivery address",
])
def test_glossary_terms_bring_their_tables(retriever, question):
    """Delivery geography lives in customer_addresses (via sales.address_id); retrieval used to miss it."""
    context = retriever.retrieve(question)
    assert "customer_addresses" in context.tables, context.tables
    assert any("customer_addresses" in j for j in context.joins)


def test_glossary_tables_exist_in_schema():
    from pantree_insights.semantic import load_glossary

    schema = load_schema()
    bad = {t: ts for t, ts in load_glossary().term_tables.items() if set(ts) - set(schema.tables)}
    assert not bad, bad
