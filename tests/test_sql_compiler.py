"""Compiler is conservative: unsupported questions must reach the existing LLM writer."""
from test_joins import fake_profile

from pantree_insights.guardrails import check_sql
from pantree_insights.joins import JoinGraph
from pantree_insights.semantic import load_schema
from pantree_insights.sql_compiler import compile_standard


def test_compile_approved_metric_and_ranked_categories():
    graph = JoinGraph(load_schema(), fake_profile())
    scope = {"table": "sales", "metrics": ["realized_revenue"], "by": {"category": "product_master.category"},
             "filters": [], "period": None, "time": "sales.placed_at",
             "supported": True, "ranking": "highest", "limit": 5}
    sql = compile_standard(scope, graph)
    assert sql is not None and "LIMIT 5" in sql and check_sql(sql).ok


def test_unsupported_query_falls_back():
    graph = JoinGraph(load_schema(), fake_profile())
    assert compile_standard({"supported": False, "metrics": ["realized_revenue"]}, graph) is None
    assert compile_standard({"supported": True, "metrics": ["realized_revenue", "orders"]}, graph) is None
