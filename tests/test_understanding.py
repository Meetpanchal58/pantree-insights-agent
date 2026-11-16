"""Question understanding, code half: IDs, values, cuts and time columns resolved without an LLM."""
import sys
from pathlib import Path

import pytest

from pantree_insights.agents import Mention, QuestionFrame
from pantree_insights.joins import JoinGraph
from pantree_insights.profile import IdFormat
from pantree_insights.semantic import load_concepts, load_schema
from pantree_insights.understanding import IdResolver, resolve

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_joins import fake_profile  # noqa: E402

CONCEPTS = load_concepts()
GRAPH = JoinGraph(load_schema(), fake_profile())
FORMATS = {"sales.order_id": IdFormat("OD", 9), "customers.customer_id": IdFormat("C", 7),
           "campaigns.campaign_id": IdFormat("CMP", 5), "product_skus.sku_id": IdFormat("P", 6, "-[A-Z0-9]+")}
# where values live (as the retriever's value index reports them)
VALUES = {"Mumbai": ["sales.ship_city", "customers.city", "customer_addresses.city", "sellers.city",
                     "warehouses.city", "product_master.brand_name"],
          "Chennai": ["sales.ship_city", "customers.city", "customer_addresses.city", "sellers.city"],
          "Watches": ["product_master.sub_category", "product_master.article_type"],
          "Footwear": ["product_master.category"], "COD": ["sales.payment_method"]}


def frame(**kw) -> QuestionFrame:
    return QuestionFrame(**{"restated_question": "q", **kw})


def run(f, choices=None, ids=None):
    return resolve(f, CONCEPTS, GRAPH, VALUES, ids or [], choices, {"cash on delivery": "COD"})


# ----------------------------------------------------------------------------- IDs
def test_ids_typed_in_full_or_as_entity_and_number():
    r = IdResolver(FORMATS, CONCEPTS.id_entities)
    found = {(m.column, m.value) for m in r.find("Why was order 12345 late? Compare with OD000000777 and "
                                                  "customer #42, campaign CMP12.")}
    assert found == {("sales.order_id", "OD000012345"), ("sales.order_id", "OD000000777"),
                     ("customers.customer_id", "C0000042"), ("campaigns.campaign_id", "CMP00012")}


def test_ids_with_the_wrong_prefix_or_too_many_digits_are_ignored():
    r = IdResolver(FORMATS, CONCEPTS.id_entities)
    assert r.find("order AB123 and customer 123456789") == []
    assert r.find("sku 123") == []                         # a sku needs its size: P000123-M
    assert r.find("Top 10 orders in 2025") == []


# ----------------------------------------------------------------------------- values and subjects
def test_same_word_resolves_by_subject():
    orders = run(frame(subjects=["orders"], mentions=[Mention(text="Mumbai", concept="city")]))
    signups = run(frame(subjects=["signups"], mentions=[Mention(text="Mumbai", concept="city")]))
    assert orders.subjects[0].filters[0].column == "sales.ship_city"
    assert signups.subjects[0].filters[0].column == "customers.city"
    assert not orders.open_points and not signups.open_points


def test_all_time_shipment_scope_requires_an_event_without_inventing_date_bounds():
    text = run(frame(subjects=["orders"], time_meaning="shipped")).to_prompt(period="all data")
    assert "Required event filter: sales.shipped_at IS NOT NULL" in text
    assert "All data means no date bounds" in text
    destination = run(frame(subjects=["orders"])).to_prompt(period="all data")
    assert "Required event filter" not in destination


def test_ambiguous_value_becomes_a_question_with_its_meanings():
    r = run(frame(subjects=["orders"], mentions=[Mention(text="Mumbai", concept="other")]))
    assert not r.subjects[0].filters
    (point,) = r.open_points
    assert point.key == "mention:Mumbai"
    assert set(point.meaning.values()) == {"city", "seller_city", "warehouse", "brand"}
    # for sign-ups only the customer's city exists, so there is nothing to ask
    assert not run(frame(subjects=["signups"], mentions=[Mention(text="Mumbai", concept="other")])).open_points


def test_an_answer_resolves_the_question():
    r = run(frame(subjects=["orders"], mentions=[Mention(text="Mumbai", concept="other")]),
            choices={"mention:Mumbai": "seller_city"})
    assert not r.open_points and r.subjects[0].filters[0].column == "sellers.city"
    assert "JOIN sellers ON sales.seller_id = sellers.seller_id" in r.subjects[0].joins


def test_meanings_in_the_same_group_are_not_worth_asking():
    r = run(frame(subjects=["orders"], mentions=[Mention(text="Watches", concept="other")]))
    assert not r.open_points and r.subjects[0].filters[0].concept == "sub_category"


def test_glossary_alias_finds_the_stored_value():
    r = run(frame(subjects=["orders"], mentions=[Mention(text="cash on delivery", concept="payment_method")]))
    assert r.subjects[0].filters[0].value == "COD"


def test_unknown_value_must_be_clarified_before_filtering():
    r = run(frame(subjects=["orders"], mentions=[Mention(text="Mumbay", concept="city")]))
    assert not r.subjects[0].filters
    point, = r.open_points
    assert point.required and "Mumbay" in point.question and "Mumbai" in point.options
    fixed = run(frame(subjects=["orders"], mentions=[Mention(text="Mumbay", concept="city")]),
                choices={point.key: "Mumbai"})
    assert fixed.subjects[0].filters[0].value == "Mumbai" and not fixed.open_points


# ----------------------------------------------------------------------------- cuts, time, joins
def test_breakdown_that_does_not_exist_offers_the_ones_that_do():
    r = run(frame(subjects=["ad_spend"], breakdowns=["city"]))
    (point,) = r.open_points
    assert "region" in point.meaning.values() and "skip" in point.meaning.values()
    fixed = run(frame(subjects=["ad_spend"], breakdowns=["city"]), choices={"breakdown:city": "region"})
    assert fixed.subjects[0].breakdowns == [("region", "marketing_spend.region")]


def test_time_column_follows_the_meaning():
    assert run(frame(subjects=["orders"])).subjects[0].time_column == "sales.placed_at"
    assert run(frame(subjects=["orders"], time_meaning="delivered")).subjects[0].time_column == "sales.delivered_at"
    assert run(frame(subjects=["signups"])).subjects[0].time_column == "customers.signup_at"


def test_two_subjects_get_their_own_columns_and_joins():
    r = run(frame(subjects=["signups", "orders"], mentions=[Mention(text="Chennai", concept="city")],
                  breakdowns=["device"]))
    by = {v.name: v for v in r.subjects}
    assert by["signups"].filters[0].column == "customers.city"
    assert by["orders"].filters[0].column == "sales.ship_city"
    assert by["signups"].breakdowns == [("device", "customers.signup_device")]
    assert by["orders"].breakdowns == [("device", "sales.device")]


def test_id_filter_uses_the_subjects_own_column():
    ids = IdResolver(FORMATS, CONCEPTS.id_entities).find("orders of customer 42")
    r = run(frame(subjects=["orders"]), ids=ids)
    assert r.subjects[0].id_filters[0].column == "sales.customer_id"          # no join to customers needed
    assert "JOIN customers" not in r.subjects[0].joins


def test_no_subject_asks_what_to_measure():
    r = run(frame(subjects=["nonsense"]))
    assert not r.subjects and r.open_points[0].key == "subject"


@pytest.mark.parametrize("subject", sorted(CONCEPTS.subjects))
def test_prompt_renders_for_every_subject(subject):
    text = run(frame(subjects=[subject])).to_prompt()
    assert f"Subject: {subject}" in text and "FROM " in text


# ----------------------------------------------------------------------------- read-back for confirmation
def test_readback_states_definitions_columns_period_and_filters():
    from pantree_insights.semantic import load_metrics
    from pantree_insights.understanding import readback

    f = frame(subjects=["orders"], measures=["late_delivery_rate"], breakdowns=["region"],
              mentions=[Mention(text="Mumbai", concept="seller_city")], period="2026-01-01 to 2026-11-14",
              restated_question="Late delivery rate by delivery region for Mumbai sellers in 2026")
    r = run(f)
    text = readback(f, r, CONCEPTS, load_metrics())
    assert "Late deliveries: Share of delivered lines that arrived after the promised date" in text
    assert "customers' / delivery region" in text                       # what 'region' means for orders
    assert "seller's city = Mumbai" in text
    assert "2026-01-01 to 2026-11-14" in text
    assert "by the date they were ordered" in text


def test_readback_for_an_unknown_measure_shows_the_words_used():
    from pantree_insights.semantic import load_metrics
    from pantree_insights.understanding import readback

    f = frame(subjects=["browsing"], measures=["average pages per visit"])
    assert "average pages per visit" in readback(f, run(f), CONCEPTS, load_metrics())


def test_case_collision_requires_an_explicit_value_choice():
    values = {"ADIDAS": ["product_master.brand_name"], "Adidas": ["product_master.brand_name"]}
    f = frame(subjects=["orders"], mentions=[Mention(text="adidas", concept="brand")])
    res = resolve(f, CONCEPTS, GRAPH, values, [])
    point, = res.open_points
    assert point.required and {"ADIDAS", "Adidas"} <= set(point.options)
    fixed = resolve(f, CONCEPTS, GRAPH, values, [], choices={point.key: "ADIDAS"})
    assert fixed.subjects[0].filters[0].value == "ADIDAS" and not fixed.open_points


def test_unknown_literal_can_be_used_explicitly_without_widening_scope():
    f = frame(subjects=["orders"], mentions=[Mention(text="Mumbay", concept="city")])
    point, = run(f).open_points
    fixed = run(f, choices={point.key: "Mumbay"})
    assert not fixed.open_points and fixed.subjects[0].filters[0].value == "Mumbay"


def test_cod_comparison_does_not_filter_out_prepaid_baseline():
    f = frame(subjects=["orders"], measures=["rto_rate"], breakdowns=["payment_method"], comparison="COD vs prepaid",
              mentions=[Mention(text="COD", concept="payment_method"),
                        Mention(text="prepaid", concept="payment_method")])
    res = run(f)
    assert not res.subjects[0].filters and not res.open_points
    assert any("prepaid" in note for note in res.notes)
