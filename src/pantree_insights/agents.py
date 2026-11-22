"""Model outputs and calls: the analyst's written answer, clarifying questions and prompt loading.

The interpreter (interpret.py) and the analyst are the only model calls. Each is one structured call: a fixed
system prompt (prompts/agents.yaml, cached by the provider) plus the question's context, returning a typed object.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

import pandas as pd
import yaml
from pydantic import BaseModel, Field

from .config import get_settings
from .grounding import Claim, render
from .llm import LLM

MAX_CLAIMS = 8


# ----------------------------------------------------------------------------- outputs
class ClarifyingQuestion(BaseModel):
    question: str = Field(description="Short question for the user")
    options: list[str] = Field(description="2-4 concrete answers to choose from")
    default: str = Field(description="The recommended option (one of options)")
    why: str = Field(description="Why the answer changes the analysis, in a few words")


class MemoryNote(BaseModel):
    statement: str = Field(description="Durable fact with its number and period, or a hypothesis")
    type: Literal["fact", "hypothesis"]


class Analysis(BaseModel):
    headline: str = Field(description="Prose with {c1}-style placeholders for every number")
    findings: list[str]
    hypotheses: list[str] = Field(default_factory=list, description="Possible explanations, explicitly unproven")
    recommendations: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    follow_up_questions: list[str] = Field(default_factory=list)
    memory: list[MemoryNote] = Field(default_factory=list)
    explains_gap_ids: list[str] = Field(default_factory=list)
    chart_title: str = ""
    chart_subtitle: str = ""
    chart_metric: str | None = Field(None, description="Result column to plot (the main metric)")
    claims: list[Claim] = Field(default_factory=list, description="Every number used in the text, as a claim")

    def texts(self) -> dict[str, str | list[str]]:
        return {"headline": self.headline, "findings": self.findings, "recommendations": self.recommendations,
                "hypotheses": self.hypotheses, "caveats": self.caveats, "memory": [m.statement for m in self.memory],
                "chart_title": self.chart_title, "chart_subtitle": self.chart_subtitle,
                "follow_up_questions": self.follow_up_questions}

    def rendered(self) -> Analysis:
        """The same analysis with placeholders replaced by the claims' formatted values."""
        r = lambda t: render(t, self.claims)                                       # noqa: E731
        return self.model_copy(update={
            "headline": r(self.headline), "findings": [r(t) for t in self.findings],
            "hypotheses": [r(t) for t in self.hypotheses],
            "recommendations": [r(t) for t in self.recommendations], "caveats": [r(t) for t in self.caveats],
            "memory": [m.model_copy(update={"statement": r(m.statement)}) for m in self.memory],
            "follow_up_questions": [r(t) for t in self.follow_up_questions],
            "chart_title": r(self.chart_title), "chart_subtitle": r(self.chart_subtitle)})


# ----------------------------------------------------------------------------- prompts
@lru_cache
def prompts() -> dict[str, str]:
    return yaml.safe_load(get_settings().prompts_file.read_text(encoding="utf-8"))


def system_prompt(role: str, **values: object) -> str:
    p = prompts()
    return p["shared"].strip() + "\n\n" + p[role].format(**values).strip()


def _section(title: str, body: str) -> str:
    return f"## {title}\n{body.strip()}" if body and body.strip() else ""


def _join(*sections: str) -> str:
    return "\n\n".join(s for s in sections if s)


def table_text(frame: pd.DataFrame, max_rows: int = 50) -> str:
    """Result table as compact CSV (numbers rounded sensibly) for prompts."""
    if frame.empty:
        return "(no rows)"
    shown = frame.head(max_rows).copy()
    for col in shown.columns:
        if pd.api.types.is_float_dtype(shown[col]):
            shown[col] = shown[col].map(lambda v: "" if pd.isna(v) else f"{v:.6g}" if abs(v) < 1e5 else f"{v:.2f}")
    extra = f"\n({len(frame) - max_rows} more rows not shown)" if len(frame) > max_rows else ""
    return shown.to_csv(index=False).strip() + extra


# ----------------------------------------------------------------------------- the analyst
def analyse(llm: LLM, question: str, reading: str, sql: str, frame: pd.DataFrame, memory: str, metric_notes: str,
            data: dict, fix_numbers: list[str] | None = None, concerns: list[str] | None = None) -> Analysis:
    """Explain the verified result; every number is a claim that code checks and renders."""
    retry = ""
    if fix_numbers:
        retry = ("Your previous answer failed the claim check:\n" + "\n".join(f"- {p}" for p in fix_numbers)
                 + "\nRewrite it: every number must be a claim whose column, row keys and op reproduce it from the "
                 "result table, and the text may contain no other numbers.")
    user = _join(
        _section("Question", question),
        _section("Confirmed reading (what was computed)", reading),
        _section("Result table", table_text(frame)),
        _section("SQL that produced it", sql),
        _section("Data", f"Data runs from {data['first_day']} to {data['last_day']} ({data['size']} shop, simulated)."),
        _section("Metric notes", metric_notes),
        _section("Facts the user must know", "\n".join(concerns or [])),
        _section("What you already know (memory)", memory),
        _section("Correction needed", retry),
    )
    answer = llm.ask("analyst", Analysis, system_prompt("analyst"), user)
    return answer.model_copy(update={"claims": answer.claims[:MAX_CLAIMS]})
