"""Graph: analysis."""
from __future__ import annotations

import re

import pandas as pd

from .. import agents
from ..grounding import Claim, KeyValue, free_numbers, unsupported_assertion
from ..interpret import canonical
from ..spec import QuerySpec


def drop_unverified(analysis: agents.Analysis, problems: list[str]) -> agents.Analysis:
    """Remove every sentence that uses a failing claim or a free-typed number; keep the rest."""
    bad = {p.split(" ", 1)[0] for p in problems if re.match(r"c\d+ ", p)}
    claims = [c for c in analysis.claims if c.id not in bad]

    def clean(text: str) -> bool:
        used = set(re.findall(r"\{(c\d+)\}", text or ""))
        return (not (used & bad) and not free_numbers(text or "") and used <= {c.id for c in claims}
                and not unsupported_assertion(text or ""))

    headline = analysis.headline if clean(analysis.headline) else \
        "The verified result is in the table below; no number in the summary could be checked."
    return analysis.model_copy(update={
        "headline": headline, "claims": claims,
        "findings": [t for t in analysis.findings if clean(t)],
        "hypotheses": [t for t in analysis.hypotheses if clean(t)],
        "recommendations": [t for t in analysis.recommendations if clean(t)],
        "caveats": [t for t in analysis.caveats if clean(t)],
        "chart_title": analysis.chart_title if clean(analysis.chart_title) else "Verified result",
        "chart_subtitle": analysis.chart_subtitle if clean(analysis.chart_subtitle) else "",
        "follow_up_questions": [t for t in analysis.follow_up_questions if clean(t)],
        "memory": [m for m in analysis.memory if clean(m.statement) and re.search(r"\{c\d+\}", m.statement)]})


def spec_key(spec: dict | None) -> str | None:
    """What a spec computes, for comparing with an intended spec (evaluation)."""
    return canonical(QuerySpec.model_validate(spec)) if spec else None


def deterministic_analysis(frame: pd.DataFrame, units: dict[str, str]) -> agents.Analysis:
    """Describe a few cells without making an untested causal, trend or ranking assertion."""
    claims, findings = [], []
    keys = [c for c in frame.columns if c not in units]
    for _, row in frame.head(5).iterrows():
        for column, unit in units.items():
            if column not in row or pd.isna(row[column]):
                continue
            claim = Claim(id=f"c{len(claims) + 1}", value=float(row[column]), unit=unit, column=column,
                          row=[KeyValue(column=k, value=str(row[k])) for k in keys])
            claims.append(claim)
            label = ", ".join(str(row[k]) for k in keys)
            # Dimension labels may contain numbers; keep labels in the result table in that case.
            label = "" if free_numbers(label) else label
            findings.append(f"{label + ': ' if label else ''}{column.replace('_', ' ')} = {{{claim.id}}}")
    return agents.Analysis(headline=findings[0] if findings else "No observed value for the requested scope.",
                           findings=findings[1:], claims=claims, chart_metric=next(iter(units), None),
                           chart_title="Verified result")
