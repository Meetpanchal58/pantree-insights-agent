"""The agents: question understanding, planner, SQL writer, validator and analyst.

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
from .grounding import Claim, render
from .llm import LLM


# ----------------------------------------------------------------------------- outputs
class ClarifyingQuestion(BaseModel):
    question: str = Field(description="Short question for the user")
    options: list[str] = Field(description="2-4 concrete answers to choose from")
    default: str = Field(description="The recommended option (one of options)")
    why: str = Field(description="Why the answer changes the analysis, in a few words")


class Mention(BaseModel):
    text: str = Field(description="The word or phrase exactly as the user wrote it, e.g. 'Mumbai', 'footwear'")
    role: Literal["filter", "comparison"] = Field(
        "filter", description="Comparison members are groups to compare, not filters that remove the baseline")
    concept: str = Field(description="A concept name from the list (city, category, brand, ...), or one of: "
                         "id, metric, period, other")


class QuestionFrame(BaseModel):
    restated_question: str = Field(description="The question as you understand it, precise and self-contained")
    subjects: list[str] = Field(default_factory=list, description="Subject names from the list: usually one; two "
                                "when the question compares them (e.g. sign-ups vs orders)")
    measures: list[str] = Field(default_factory=list, description="What to measure: metric names or exact words")
    mentions: list[Mention] = Field(default_factory=list, description="Every value-like word in the question")
    breakdowns: list[str] = Field(default_factory=list, description="Concept names to split the result by")
    period: str = Field("all data", description="Explicit dates (YYYY-MM-DD to YYYY-MM-DD) or 'all data'")
    time_meaning: str | None = Field(None, description="Which date of the subject, from its time meanings "
                                     "(e.g. 'delivered'); empty = the subject's default date")
    comparison: str | None = Field(None, description="Baseline to compare with, if any")
    ranking: Literal["highest", "lowest"] | None = Field(None, description="Only for a requested ranking")
    row_limit: int | None = Field(None, ge=1, le=500, description="Requested top/bottom N; ranking without N = 10")
    open_points: list[ClarifyingQuestion] = Field(default_factory=list, description="Everything still unclear "
                                                  "that changes the numbers; empty when the question is clear")


class AnalysisPlan(BaseModel):
    decision: Literal["proceed", "clarify", "decline"]
    decline_reason: str | None = Field(None, description="Only for decline: why, plus an aggregated alternative")
    clarifying_questions: list[ClarifyingQuestion] = Field(default_factory=list,
                                                           description="Only for clarify")
    restated_question: str = Field(description="The question as you will answer it, precise and self-contained")
    intent: str = Field("", description="What the stakeholder wants to learn, in one short phrase")
    source_tables: list[str] = Field(default_factory=list, description="Tables the SQL must use (from the context)")
    join_path: list[str] = Field(default_factory=list, description="Approved joins from the context, "
                                 "e.g. sales.product_id = product_master.product_id")
    metrics: list[str] = Field(default_factory=list, description="metrics.yaml names, or exact formulas")
    breakdowns: list[str] = Field(default_factory=list, description="Analytical cuts, at most 2")
    filters: list[str] = Field(default_factory=list, description="Exact-value filters, e.g. category = 'Footwear'")
    time_window: str = Field("", description="Explicit dates, e.g. 2026-01-01 to 2026-11-14 (inclusive)")
    comparison: str | None = Field(None, description="Baseline to compare with, if any")
    result_grain: str = Field("", description="One row per ... (the result's dimensions), e.g. one row per category")
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
    headline: str = Field(description="Prose with {c1}-style placeholders for every number")
    findings: list[str]
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
                "caveats": self.caveats, "memory": [m.statement for m in self.memory]}

    def rendered(self) -> Analysis:
        """The same analysis with placeholders replaced by the claims' formatted values."""
        r = lambda t: render(t, self.claims)                                       # noqa: E731
        return self.model_copy(update={
            "headline": r(self.headline), "findings": [r(t) for t in self.findings],
            "recommendations": [r(t) for t in self.recommendations], "caveats": [r(t) for t in self.caveats],
            "memory": [m.model_copy(update={"statement": r(m.statement)}) for m in self.memory],
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


def plan_text(plan: AnalysisPlan) -> str:
    lines = [f"Question: {plan.restated_question}", f"Intent: {plan.intent or '-'}",
             f"Source tables: {', '.join(plan.source_tables) or '-'}",
             f"Join path: {'; '.join(plan.join_path) or 'none (single table)'}",
             f"Result grain: {plan.result_grain or '-'}",
             f"Metrics: {', '.join(plan.metrics) or '-'}",
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
def understand(llm: LLM, question: str, context: str, data: dict, concepts, history: str = "",
               answers: dict[str, str] | None = None, notes: list[str] | None = None) -> QuestionFrame:
    """What exactly is being asked: subjects, measures, mentions, cuts, period, and what is still unclear."""
    subjects = "\n".join(f"- {n} ({s.table}): {s.about}" + (f"; dates: {', '.join(s.time_meanings)}"
                                                             if s.time_meanings else "")
                         for n, s in concepts.subjects.items())
    cuts = "\n".join(f"- {c}: {concepts.label(c)} (for: {', '.join(e)})" for c, e in concepts.concepts.items())
    answered = "\n".join(f"- {q}: {a}" for q, a in (answers or {}).items())
    user = _join(
        _section("Earlier in this chat", history),
        _section("Question", question),
        _section("The user's answers so far", answered),
        _section("Problems found in your last reading (resolve them or ask)", "\n".join(f"- {n}" for n in notes or [])),
        _section("Data", f"Shop data from {data['first_day']} to {data['last_day']} ({data['size']} shop)."),
        _section("Subjects (what a question can be about)", subjects),
        _section("Concepts (cuts and filters, and the subjects they exist for)", cuts),
        _section("Context (tables, metrics, values, examples)", context),
    )
    system = system_prompt("understander", data_last_day=data["last_day"], data_first_day=data["first_day"])
    return llm.ask("understander", QuestionFrame, system, user)


def plan(llm: LLM, question: str, context: str, memory: str, data: dict, history: str = "",
         answers: dict[str, str] | None = None, use_defaults: bool = False, understanding: str = "") -> AnalysisPlan:
    system = system_prompt("planner", data_last_day=data["last_day"], data_first_day=data["first_day"])
    answered = "\n".join(f"- {q}: {a}" for q, a in (answers or {}).items())
    user = _join(
        _section("Earlier in this chat", history),
        _section("Question", question),
        _section("The user's answers to clarifying questions", answered),
        _section("Question understanding", understanding),
        _section("Instruction", "The user asked you to use sensible defaults: proceed without asking."
                 if use_defaults else ""),
        _section("Data", f"Shop data from {data['first_day']} to {data['last_day']} ({data['size']} shop)."),
        _section("What the agent remembers", memory),
        _section("Context (tables, metrics, values, examples)", context),
    )
    result = llm.ask("planner", AnalysisPlan, system, user)
    if result.decision == "clarify":
        # Previously answered questions can be suppressed; new business ambiguities must reach the user.
        answered = {q.strip().casefold() for q in (answers or {})}
        result.clarifying_questions = [q for q in result.clarifying_questions
                                      if q.question.strip().casefold() not in answered]
        if use_defaults:
            result.assumptions += [f"Assumed '{q.default}' for: {q.question}" for q in result.clarifying_questions]
            result.clarifying_questions = []
        if not result.clarifying_questions:
            result.decision = "proceed"
    return result


def write_sql(llm: LLM, question: str, the_plan: AnalysisPlan, context: str,
              feedback: list[str] | None = None, previous_sql: str | None = None,
              understanding: str = "", references: str = "") -> SQLDraft:
    settings = get_settings()
    system = system_prompt("sql_writer", max_rows=settings.max_rows)
    retry = ""
    if feedback:
        retry = f"Previous SQL:\n{previous_sql}\n\nProblems to fix:\n" + "\n".join(f"- {p}" for p in feedback)
    user = _join(
        _section("Analysis plan", plan_text(the_plan)),
        _section("Original question", question),
        _section("Question understanding (filters, columns and joins resolved in code: follow exactly)",
                 understanding),
        _section("Fix the previous attempt", retry),
        _section("Context (tables, joins, metrics, values, examples)", context),
        _section("Reference queries with the same tables, columns and metrics (verified patterns to adapt)",
                 references),
    )
    return llm.ask("sql_writer", SQLDraft, system, user, effort=settings.sql_reasoning_effort)


def validate(llm: LLM, question: str, the_plan: AnalysisPlan, sql: str, preview: pd.DataFrame,
             metric_defs: str, understanding: str = "") -> Verdict:
    user = _join(
        _section("Question", question),
        _section("Question understanding (resolved in code)", understanding),
        _section("Analysis plan", plan_text(the_plan)),
        _section("SQL", sql),
        _section("Result preview", table_text(preview, 15) + f"\n(total rows: {len(preview)})"),
        _section("Metric definitions", metric_defs),
    )
    return llm.ask("validator", Verdict, system_prompt("validator"), user)


def analyse(llm: LLM, question: str, the_plan: AnalysisPlan, sql: str, frame: pd.DataFrame, memory: str,
            metric_notes: str, data: dict, fix_numbers: list[str] | None = None,
            concerns: list[str] | None = None) -> Analysis:
    retry = ""
    if fix_numbers:
        retry = ("Your previous answer failed the claim check:\n" + "\n".join(f"- {p}" for p in fix_numbers)
                 + "\nRewrite it: every number must be a claim whose column, row keys and op reproduce it from the "
                 "result table, and the text may contain no other numbers.")
    user = _join(
        _section("Question", question),
        _section("Analysis plan", plan_text(the_plan)),
        _section("Result table", table_text(frame)),
        _section("SQL that produced it", sql),
        _section("Data", f"Data runs from {data['first_day']} to {data['last_day']} ({data['size']} shop, simulated)."),
        _section("Metric notes", metric_notes),
        _section("Validation concerns (explain limitations; do not make confident recommendations)",
                 "\n".join(concerns or [])),
        _section("What you already know (memory)", memory),
        _section("Correction needed", retry),
    )
    return llm.ask("analyst", Analysis, system_prompt("analyst"), user)
