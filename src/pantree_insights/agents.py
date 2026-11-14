"""The four agents: planner, SQL writer, validator and analyst.

Each agent is one structured LLM call: a fixed system prompt (prompts/agents.yaml) plus the question's
context, returning a typed pydantic object. Orchestration (retries, clarifying questions, memory)
lives in graph.py.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

import pandas as pd
import yaml
from pydantic import BaseModel, Field

from .config import get_settings
from .llm import LLM


# ----------------------------------------------------------------------------- outputs
class ClarifyingQuestion(BaseModel):
    question: str = Field(description="Short question for the user")
    options: list[str] = Field(description="2-4 concrete answers to choose from")
    default: str = Field(description="The recommended option (one of options)")
    why: str = Field(description="Why the answer changes the analysis, in a few words")


class AnalysisPlan(BaseModel):
    decision: Literal["proceed", "clarify", "decline"]
    decline_reason: str | None = Field(None, description="Only for decline: why, plus an aggregated alternative")
    clarifying_questions: list[ClarifyingQuestion] = Field(default_factory=list,
                                                           description="Only for clarify; max 3")
    restated_question: str = Field(description="The question as you will answer it, precise and self-contained")
    metrics: list[str] = Field(default_factory=list, description="metrics.yaml names, or exact formulas")
    breakdowns: list[str] = Field(default_factory=list, description="Analytical cuts, at most 2")
    filters: list[str] = Field(default_factory=list, description="Exact-value filters, e.g. category = 'Footwear'")
    time_window: str = Field("", description="Explicit dates, e.g. 2026-01-01 to 2026-11-14 (inclusive)")
    comparison: str | None = Field(None, description="Baseline to compare with, if any")
    output_shape: str = Field("", description="Expected result: one row per ..., with columns ...")
    assumptions: list[str] = Field(default_factory=list)


class SQLDraft(BaseModel):
    sql: str = Field(description="One DuckDB SELECT statement")
    explanation: str = Field(description="One or two sentences: what the query computes")


class Verdict(BaseModel):
    ok: bool
    problems: list[str] = Field(default_factory=list, description="Concrete, fixable problems if not ok")


class MemoryNote(BaseModel):
    statement: str = Field(description="Durable fact with its number and period, or a hypothesis")
    type: Literal["fact", "hypothesis"]


class Analysis(BaseModel):
    headline: str
    findings: list[str]
    recommendations: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    follow_up_questions: list[str] = Field(default_factory=list)
    memory: list[MemoryNote] = Field(default_factory=list)
    explains_gap_ids: list[str] = Field(default_factory=list)
    chart_title: str = ""
    chart_subtitle: str = ""
    chart_metric: str | None = Field(None, description="Result column to plot (the main metric)")


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


def plan_text(plan: AnalysisPlan) -> str:
    lines = [f"Question: {plan.restated_question}", f"Metrics: {', '.join(plan.metrics) or '-'}",
             f"Breakdowns: {', '.join(plan.breakdowns) or 'none'}", f"Filters: {'; '.join(plan.filters) or 'none'}",
             f"Time window: {plan.time_window or 'all data'}", f"Comparison: {plan.comparison or 'none'}",
             f"Expected result: {plan.output_shape or '-'}"]
    if plan.assumptions:
        lines.append("Assumptions: " + "; ".join(plan.assumptions))
    return "\n".join(lines)


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


# ----------------------------------------------------------------------------- the agents
def plan(llm: LLM, question: str, context: str, memory: str, data: dict, history: str = "",
         answers: dict[str, str] | None = None, use_defaults: bool = False) -> AnalysisPlan:
    system = system_prompt("planner", data_last_day=data["last_day"], data_first_day=data["first_day"])
    answered = "\n".join(f"- {q}: {a}" for q, a in (answers or {}).items())
    user = _join(
        _section("Earlier in this chat", history),
        _section("Question", question),
        _section("The user's answers to your clarifying questions", answered),
        _section("Instruction", "The user asked you to use sensible defaults: proceed without asking."
                 if use_defaults else ""),
        _section("Data", f"Shop data from {data['first_day']} to {data['last_day']} ({data['size']} shop)."),
        _section("What the agent remembers", memory),
        _section("Context (tables, metrics, values, examples)", context),
    )
    result = llm.ask("planner", AnalysisPlan, system, user)
    result.clarifying_questions = result.clarifying_questions[:3]
    if answers or use_defaults:
        if result.decision == "clarify":           # never ask twice
            result.decision = "proceed"
            result.assumptions += [f"Assumed '{q.default}' for: {q.question}" for q in result.clarifying_questions]
        result.clarifying_questions = []
    return result


def write_sql(llm: LLM, question: str, the_plan: AnalysisPlan, context: str,
              feedback: list[str] | None = None, previous_sql: str | None = None) -> SQLDraft:
    settings = get_settings()
    system = system_prompt("sql_writer", max_rows=settings.max_rows)
    retry = ""
    if feedback:
        retry = f"Previous SQL:\n{previous_sql}\n\nProblems to fix:\n" + "\n".join(f"- {p}" for p in feedback)
    user = _join(
        _section("Analysis plan", plan_text(the_plan)),
        _section("Original question", question),
        _section("Fix the previous attempt", retry),
        _section("Context (tables, joins, metrics, values, examples)", context),
    )
    return llm.ask("sql_writer", SQLDraft, system, user, effort=settings.sql_reasoning_effort)


def validate(llm: LLM, question: str, the_plan: AnalysisPlan, sql: str, preview: pd.DataFrame,
             metric_defs: str) -> Verdict:
    user = _join(
        _section("Question", question),
        _section("Analysis plan", plan_text(the_plan)),
        _section("SQL", sql),
        _section("Result preview", table_text(preview, 15) + f"\n(total rows: {len(preview)})"),
        _section("Metric definitions", metric_defs),
    )
    return llm.ask("validator", Verdict, system_prompt("validator"), user)


def analyse(llm: LLM, question: str, the_plan: AnalysisPlan, sql: str, frame: pd.DataFrame, memory: str,
            metric_notes: str, data: dict, fix_numbers: list[str] | None = None) -> Analysis:
    retry = ""
    if fix_numbers:
        retry = ("Your previous answer used numbers that are not in the result table: "
                 + ", ".join(fix_numbers) + ". Rewrite using only numbers from the table (or simple derivations).")
    user = _join(
        _section("Question", question),
        _section("Analysis plan", plan_text(the_plan)),
        _section("Result table", table_text(frame)),
        _section("SQL that produced it", sql),
        _section("Data", f"Data runs from {data['first_day']} to {data['last_day']} ({data['size']} shop, simulated)."),
        _section("Metric notes", metric_notes),
        _section("What you already know (memory)", memory),
        _section("Correction needed", retry),
    )
    return llm.ask("analyst", Analysis, system_prompt("analyst"), user)
