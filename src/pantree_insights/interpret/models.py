"""Interpret: models."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from ..agents import ClarifyingQuestion
from ..context import ResultReference
from ..spec import Filter, Measure, Operation, QuerySpec, Ranking, TimeSpec
from ..templates import Event

MAX_REPAIRS = 1


MAX_ALTERNATIVES = 2


CANNOT_BUILD = "I could not build this exactly"


READINGS = "I can read this question in more than one way"


def is_confirmation(message: str) -> bool:
    """Full matches only: 'yes, but change the dates' is a revision, never approval."""
    return bool(re.fullmatch(r"(?:yes(?:,? (?:please|go ahead))?|proceed|looks correct|go ahead|"
                             r"yes, go ahead|confirm(?:ed)?|that(?:'s| is) correct|run (?:it|this plan))"
                             r"[.!]?", message.strip(), re.I))


class TemplateCall(BaseModel):
    """A template and its parameters. Every parameter is a declared field (providers' strict structured output
    rejects free-form objects); unset ones take the template's default."""
    name: Literal["event_lift", "channel_roas", "platform_vs_recorded_orders", "repeat_within_days",
                  "cohort_retention", "session_funnel", "customer_segments"]
    days_before: int | None = Field(None, description="event_lift: baseline days before each event")
    by: Literal["channel", "channel_group", "device", "traffic_source"] | None = Field(
        None, description="Supported split for the chosen template")
    days: int | None = Field(None, description="repeat_within_days: window after the first order")
    start: date | None = None
    end: date | None = None
    as_of: date | None = None
    grain: Literal["month", "quarter", "year"] | None = None
    steps: list[Event] | None = None
    min_orders: int | None = None
    recency_days: int | None = None
    filters: list[Filter] | None = None

    @model_validator(mode="before")
    @classmethod
    def _flatten(cls, data):
        if isinstance(data, dict) and isinstance(data.get("params"), dict):     # {"name", "params": {...}}
            data = {k: v for k, v in data.items() if k != "params"} | data["params"]
        return data

    @property
    def params(self) -> dict:
        return {k: v for k, v in self.model_dump().items() if k != "name" and v is not None}


class AnalysisStep(BaseModel):
    purpose: str = Field(description="Why this query helps the business objective")
    spec: QuerySpec | None = None
    template: TemplateCall | None = None

    @model_validator(mode="after")
    def _one_plan(self):
        if (self.spec is None) == (self.template is None):
            raise ValueError("each investigation step needs exactly one spec or template")
        return self


class Interpretation(BaseModel):
    adaptive: bool = Field(False, description="Enable evidence-directed follow-up probes only when the user requests "
                          "an investigation, root-cause analysis, or testing competing explanations")
    result_scope: ResultReference | None = Field(None, description="For these/those/same groups: reference a verified "
                                               "population. Never invent its member values.")
    relationship: Literal["new", "refinement", "drilldown", "comparison", "investigation"] = Field(
        "new", description="Relationship to the saved business objective; use new only for an independent question")
    decline_reason: str | None = Field(None, description="Only if the request must be declined; decided first")
    decline_category: Literal["personal_data", "raw_dump", "change_data", "instructions", "off_topic"] | None = None
    route: Literal["spec", "template", "investigation", "clarify", "unanswerable"] = Field(
        "spec", description="spec: one QuerySpec; template: one of the listed analyses; clarify: something material "
        "is open; unanswerable: the data cannot answer it as asked (say what it can answer instead)")
    restated_question: str = Field(description="The question as you will answer it, precise and self-contained")
    spec: QuerySpec | None = Field(None, description="For route spec")
    template: TemplateCall | None = Field(None, description="For route template")
    questions: list[ClarifyingQuestion] = Field(default_factory=list, description="For route clarify")
    assumptions: list[str] = Field(default_factory=list, description="Defaults you applied, in business words")
    unanswerable_reason: str | None = Field(None, description="For route unanswerable: why, and what can be answered")
    steps: list[AnalysisStep] = Field(default_factory=list, max_length=4,
                                     description="Bounded queries proposed together for confirmation")


class Alternatives(BaseModel):
    readings: list[QuerySpec] = Field(default_factory=list, description="Materially different readings, at most 2")
    why: list[str] = Field(default_factory=list, description="For each reading: the word it reads differently")

    @model_validator(mode="before")
    @classmethod
    def _drop_malformed(cls, data):
        """A malformed alternative is dropped, not the whole answer: the chosen reading already passed every check,
        and an alternative that is not a valid spec could never be offered anyway."""
        if isinstance(data, dict) and isinstance(data.get("readings"), list):
            keep = []
            for reading in data["readings"]:
                try:
                    keep.append(QuerySpec.model_validate(reading))
                except (ValueError, TypeError):
                    continue
            data = {**data, "readings": keep}
        return data


class SpecPatch(BaseModel):
    """Only explicitly changed fields; omitted/null fields preserve the prior plan."""
    measures: list[Measure] | None = None
    dimensions: list[str] | None = None
    filters: list[Filter] | None = None
    time: TimeSpec | None = None
    ranking: Ranking | None = None
    operation: Operation | None = None
    shares: list[str] | None = None
    clear: list[Literal["time", "ranking", "operation"]] = Field(default_factory=list)
    question: str | None = Field(None, description="Ask one focused question if the correction is ambiguous")


@dataclass
class Decision:
    kind: Literal["spec", "template", "investigation", "clarify", "decline", "unanswerable", "failed"]
    spec: QuerySpec | None = None
    template: TemplateCall | None = None
    questions: list[ClarifyingQuestion] = field(default_factory=list)
    required: bool = False                       # a question no default may answer
    restated: str = ""
    assumptions: list[str] = field(default_factory=list)
    reason: str = ""
    readings: list[QuerySpec] = field(default_factory=list)    # options of a READINGS question, in order
    detail: dict = field(default_factory=dict)
    steps: list[AnalysisStep] = field(default_factory=list)
