"""Interpret: readback."""
from __future__ import annotations

from ..spec import TICKETS, Catalog, QuerySpec


def readback(spec: QuerySpec, cat: Catalog, assumptions: list[str] | None = None) -> str:
    def label(field: str) -> str:
        if "." in field:
            return field.split(".", 1)[1].replace("_", " ")
        return cat.concepts.label(field)

    def show(f) -> str:
        if f.op == "is_set":
            return f"{label(f.field)} is recorded"
        if f.op == "is_not_set":
            return f"{label(f.field)} is not recorded"
        if f.op.endswith("_field"):
            return f"{label(f.field)} {f.op.split('_')[0].replace('lte', 'at most').replace('gte', 'at least')} " \
                   f"{label(f.other or '')}"
        if f.op in ("lt", "lte", "gt", "gte"):
            return f"{label(f.field)} {dict(lt='<', lte='≤', gt='>', gte='≥')[f.op]} {f.values[0]}"
        words = " or ".join(str(v) for v in f.values)
        return f"{label(f.field)} {'is not' if f.op == 'not_in' else 'is'} {words}"

    entity = cat.catalog.get(spec.table, {}).get("entity", "ticket" if spec.table == TICKETS else spec.table)
    lines = []
    if spec.columns:
        lines.append(f"**List:** {', '.join(label(c) for c in spec.columns)} of each {entity} "
                     f"(at most {spec.limit or 100})")
    for m in spec.measures:
        if m.metric:
            metric = cat.metrics.metrics[m.metric]
            text = f"{metric.label}: {metric.description}"
            if m.where:
                text += " where " + " and ".join(show(f) for f in m.where)
        elif m.ratio:
            text = f"{m.name.replace('_', ' ')} = {m.ratio[0].replace('_', ' ')} / {m.ratio[1].replace('_', ' ')}"
        elif m.agg == "share":
            text = f"{m.name.replace('_', ' ')}: share of {entity} rows where " + " and ".join(show(f) for f in m.where)
        else:
            what = "number of rows" if m.agg == "count" else f"{m.agg.replace('_', ' ')} of {label(m.field or '')}"
            cond = (" where " + " and ".join(show(f) for f in m.where)) if m.where else ""
            text = f"{m.name.replace('_', ' ')}: {what}{cond} (one row = {entity})"
        lines.append(f"**Measure:** {text}")
    splits = [label(d) for d in spec.dimensions] + ([spec.time.grain] if spec.time and spec.time.grain else [])
    if splits:
        lines.append(f"**Split by:** {', '.join(splits)}")
    if spec.time and (spec.time.start or spec.time.end):
        when = f"{spec.time.start or 'the start of the data'} to {spec.time.end or 'the latest data'}"
        lines.append(f"**Period:** {when}" + (f", by {label(spec.time.field)}" if spec.time.field else ""))
    else:
        lines.append("**Period:** all available data")
    lines.append("**Filters:** " + ("; ".join(show(f) for f in spec.filters) if spec.filters else "none"))
    if spec.ranking:
        lines.append(f"**Ranking:** {'top' if spec.ranking.order == 'desc' else 'bottom'} {spec.ranking.limit} "
                     f"by {spec.ranking.by.replace('_', ' ')}" +
                     (" within each " + ", ".join(label(d) for d in spec.ranking.partition_by)
                      if spec.ranking.partition_by else " overall"))
    if spec.shares:
        lines.append("**Also shown:** " + ("contribution to net change for " if spec.operation else
                     "share of the total for ") + ", ".join(s.replace("_", " ") for s in spec.shares))
    if spec.operation:
        op = spec.operation
        description = (f"compared with {op.previous_start} to {op.previous_end}; absent populations remain unknown"
                       if op.kind == "period_compare" else
                       f"{op.days}-calendar-day rolling sum including lookback; observed days only"
                       if op.kind == "rolling_sum" else "cumulative sum from the requested start; observed days only")
        lines.append("**Calculation:** " + description)
    if assumptions:
        lines.append("**Assumed:** " + "; ".join(assumptions))
    return "\n".join(lines)
