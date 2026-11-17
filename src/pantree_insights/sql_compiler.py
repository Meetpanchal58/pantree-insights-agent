"""Conservative deterministic SQL generation for one pre-defined metric on an approved grain.

Not a replacement for general text-to-SQL: unsupported periods, comparisons, custom
metrics, complex queries and unsafe join paths always fall back to the existing writer.
Enable via ENABLE_SQL_COMPILER=true only after measuring the live evaluation suite.
"""
from __future__ import annotations

from .guardrails import check_sql
from .joins import JoinGraph
from .recompute import canonical_sql


def compile_standard(scope: dict | None, graph: JoinGraph) -> str | None:
    if not scope or not scope.get("supported") or len(scope.get("metrics", [])) != 1:
        return None
    if not scope.get("table") or not scope.get("time"):
        return None
    ranking = scope.get("ranking")
    if ranking and not scope.get("by"):
        return None
    metric = scope["metrics"][0]
    sql = canonical_sql(metric, graph, by=scope.get("by") or {},
                        filters=[tuple(f) for f in scope.get("filters") or []],
                        period=tuple(scope["period"]) if scope.get("period") else None,
                        time_column=scope.get("time"), conditions=scope.get("conditions"))
    if not sql:
        return None
    # The canonical builder owns metric logic, cardinality-safe joins and period predicates.
    # The wrapper only gives the result a stable semantic metric name and optional ranking.
    dims = list((scope.get("by") or {}).keys())
    projection = ", ".join([*dims, f"value AS {metric}"])
    result = f"SELECT {projection} FROM ({sql}) AS canonical"
    if ranking:
        direction = "DESC" if ranking == "highest" else "ASC"
        limit = scope.get("limit") or 10
        if not isinstance(limit, int) or not 1 <= limit <= 500:
            return None
        result += f" ORDER BY {metric} {direction} NULLS LAST LIMIT {limit}"
    verdict = check_sql(result)
    return verdict.sql if verdict.ok else None
