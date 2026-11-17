from pantree_insights.compiler import compile_metric_query
from pantree_insights.guardrails import check_sql
from pantree_insights.joins import JoinGraph
from pantree_insights.profile import JoinProfile, Profile
from pantree_insights.semantic import load_schema


def graph() -> JoinGraph:
    joins = {}
    for join in load_schema().joins:
        left, right = (2, 1)
        joins[join] = JoinProfile(join, left, right, 0.0, 0.0)
    return JoinGraph(load_schema(), Profile(tables={}, joins=joins))


def test_compiles_supported_single_metric_without_llm():
    scope = {
        "table": "sales",
        "metrics": ["orders"],
        "by": {"payment_method": "sales.payment_method"},
        "filters": [],
        "period": None,
        "time": "sales.placed_at",
        "supported": True,
        "ranking": None,
        "limit": None,
    }
    query = compile_metric_query(scope, graph())
    assert query is not None
    assert "orders" in query.sql.lower()
    assert "payment_method" in query.sql.lower()
    assert check_sql(query.sql).ok


def test_refuses_complex_or_wrong_subject_scope():
    join_graph = graph()
    assert compile_metric_query({"supported": False, "metrics": ["orders"]}, join_graph) is None
    assert compile_metric_query({
        "supported": True, "table": "customers", "metrics": ["orders"], "by": {}, "filters": [],
        "period": None, "time": "customers.created_at",
    }, join_graph) is None
    assert compile_metric_query({
        "supported": True, "table": "sales", "metrics": ["orders", "realized_revenue"], "by": {}, "filters": [],
        "period": None, "time": "sales.placed_at",
    }, join_graph) is None
