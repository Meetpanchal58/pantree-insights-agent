"""Deterministic SQL compilation for simple, fully resolved metric queries.

The LLM still interprets intent. When understanding has resolved one supported metric,
one subject, dimensions, filters and a period, code builds the SQL from metrics.yaml
instead of asking the model to invent SQL. Complex/comparison analyses fall back to
the existing SQL writer.
"""
from __future__ import annotations

from dataclasses import dataclass

from .joins import JoinGraph
from .recompute import canonical_sql
from .semantic import load_metrics


@dataclass(frozen=True)
class CompiledQuery:
    sql: str
    metric: str
    reason: str = "compiled from the confirmed metric definition"


def compile_metric_query(scope: dict | None, joins: JoinGraph) -> CompiledQuery | None:
    if not scope or not scope.get("supported", True):
        return None
    metrics = list(scope.get("metrics") or [])
    if len(metrics) != 1:
        return None
    metric = metrics[0]
    definitions = load_metrics()
    definition = definitions.metrics.get(metric)
    if not definition:
        return None
    source_table = definitions.sources[definition.source].from_.split()[0]
    if source_table != scope.get("table"):
        return None
    period = tuple(scope["period"]) if scope.get("period") else None
    sql = canonical_sql(
        metric,
        joins,
        by=scope.get("by") or {},
        filters=[tuple(x) for x in scope.get("filters") or []],
        period=period,
        time_column=scope.get("time"),
    )
    if not sql:
        return None

    keys = list((scope.get("by") or {}).keys())
    projections = [*keys, f"value AS {metric}"]
    wrapped = f"SELECT {', '.join(projections)} FROM ({sql}) AS compiled"
    if scope.get("ranking"):
        direction = "ASC" if str(scope["ranking"]).lower().startswith(("bottom", "lowest", "asc")) else "DESC"
        limit = int(scope.get("limit") or 10)
        wrapped += f" ORDER BY {metric} {direction} LIMIT {limit}"
    return CompiledQuery(wrapped, metric)
