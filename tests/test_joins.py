"""Join graph: join paths are built in code from the approved joins and the measured cardinality."""
import pytest

from pantree_insights.joins import JoinGraph
from pantree_insights.profile import JoinProfile, Profile
from pantree_insights.semantic import load_schema

# Measured on the full shop (pantree-profile); a fixture so these tests need no data.
MANY_TO_MANY = {"sales.coupon_code = promotions.code", "sales.order_id = marketing_attribution.order_id",
                "clickstream.session_id = sales.session_id"}
ONE_TO_ONE = {"sales.transaction_id = product_reviews.transaction_id", "inventory_live.sku_id = product_skus.sku_id",
              "customers.customer_id = customer_addresses.customer_id"}
ONE_TO_MANY = {"sales.transaction_id = support_contacts.transaction_id"}


def fake_profile() -> Profile:
    joins = {}
    for j in load_schema().joins:
        left, right = (2, 2) if j in MANY_TO_MANY else (1, 1) if j in ONE_TO_ONE else (1, 2) if j in ONE_TO_MANY \
            else (2, 1)
        joins[j] = JoinProfile(j, left, right, 0.0, 0.0)
    return Profile(tables={}, joins=joins)


@pytest.fixture(scope="module")
def graph():
    return JoinGraph(load_schema(), fake_profile())


def test_direct_many_to_one_joins_from_the_fact_table(graph):
    path = graph.path("sales", ["product_master", "sale_events"])
    assert [s.join for s in path] == ["sales.product_id = product_master.product_id",
                                      "sales.event_id = sale_events.event_id"]
    assert not any(s.fans_out for s in path)


def test_bridge_tables_are_added(graph):
    path = graph.path("marketing_spend", ["sale_events"])
    assert [s.right for s in path] == ["campaigns", "sale_events"]


def test_delivery_address_uses_the_direct_join_not_a_detour(graph):
    path = graph.path("sales", ["customer_addresses"])
    assert [s.join for s in path] == ["sales.address_id = customer_addresses.address_id"]


def test_fan_out_is_flagged_and_avoided_when_possible(graph):
    path = graph.path("product_master", ["sales"])
    assert path[0].fans_out                                   # one product -> many sale lines
    to_attr = graph.path("sales", ["marketing_attribution"])
    assert to_attr[0].fans_out                                # N:N on order_id
    assert "aggregate" in graph.render("sales", ["marketing_attribution"]).lower()


def test_join_rule_extra_condition_is_part_of_the_step(graph):
    step = graph.path("sales", ["promotions"])[0]
    assert "start_date" in step.on and "end_date" in step.on


def test_render_lists_from_and_joins(graph):
    text = graph.render("sales", ["customer_addresses", "product_master"])
    assert text.splitlines()[0].startswith("FROM sales")
    assert "JOIN customer_addresses ON sales.address_id = customer_addresses.address_id" in text


def test_unknown_table_raises(graph):
    with pytest.raises(KeyError):
        graph.path("sales", ["customer_pii"])


def test_ties_prefer_joining_from_the_root(graph):
    """sales.campaign_id (the order's campaign) and marketing_attribution.campaign_id (a touchpoint) are both one
    hop away; the root table's own join must win."""
    path = graph.path("sales", ["marketing_attribution", "campaigns"])
    assert "sales.campaign_id = campaigns.campaign_id" in [s.join for s in path]


def test_join_rule_measured_with_its_condition_is_not_flagged():
    profile = fake_profile()
    profile.joins["sales.coupon_code = promotions.code"] = JoinProfile("sales.coupon_code = promotions.code", 2, 1,
                                                                       0.0, 0.0)
    step = JoinGraph(load_schema(), profile).path("sales", ["promotions"])[0]
    assert not step.fans_out
