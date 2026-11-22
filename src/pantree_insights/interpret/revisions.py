"""Interpret: revisions."""
from __future__ import annotations

import json
import re

from ..agents import ClarifyingQuestion
from ..fastpath import followup
from ..spec import Catalog, QuerySpec, validate
from ..templates import TemplateError
from ..templates import build as build_template
from .checks import intent_problems, template_intent_problems
from .models import Decision, SpecPatch, TemplateCall
from .prompts import catalog_text
from .readings import normalise


def revise(llm, cat: Catalog, previous: QuerySpec, correction: str, data: dict) -> Decision:
    """Apply a minimal edit; keep unchanged dates, populations and metric definitions byte-for-byte."""
    text = re.sub(r"^(?:no[, ]+|instead[, ]+)", "", correction.strip(), flags=re.I)
    text = re.sub(r"\s+(?:instead|please)[.!?]?$", "", text, flags=re.I)
    direct = followup(text, previous, cat, data["last_day"])
    deterministic = direct is not None
    if direct is None:
        patch = llm.ask("interpreter", SpecPatch,
            "Revise only the explicitly changed fields of this governed analytical plan. Null means unchanged. "
            "Preserve every other metric, filter, period and operation. Lists replace that field entirely, so retain "
            "its unaffected entries. Never write SQL. If scope is ambiguous, return question and no edits.\n" +
            catalog_text(cat, {}), json.dumps({"previous": previous.model_dump(mode="json"),
                                             "correction": correction, "data": data}))
        if patch.question:
            return Decision("clarify", questions=[ClarifyingQuestion(question=patch.question, options=[],
                            default="", why="The change affects the calculation")], required=True)
        changes = patch.model_dump(exclude_none=True, exclude={"clear", "question"})
        changes.update({field: None for field in patch.clear})
        if patch.time and previous.time and "time" not in patch.clear:
            changes["time"] = {**previous.time.model_dump(), **patch.time.model_dump(exclude_none=True)}
        direct = QuerySpec.model_validate({**previous.model_dump(), **changes})
    direct = normalise(direct, cat, {})
    problems = validate(direct, cat) or intent_problems(correction, direct, cat, data["last_day"])
    if problems:
        return Decision("clarify", questions=[ClarifyingQuestion(question="; ".join(problems) + ". What should change?",
                        options=[], default="", why="The revised plan must preserve your scope")], required=True)
    return Decision("spec", spec=direct, restated="update the analysis as requested: " + correction,
                    detail={"fast_path": deterministic, "revised": True})


def revise_template(llm, cat: Catalog, previous: TemplateCall, correction: str, data: dict) -> Decision:
    patch = llm.ask("interpreter", TemplateCall,
        "Edit only explicitly changed parameters of the previous analytical template. Keep its name; null means "
        "unchanged. Dates, filters and populations not mentioned must be preserved. Never write SQL.\n" +
        catalog_text(cat, {}), json.dumps({"previous": previous.model_dump(mode="json"), "correction": correction}))
    if patch.name != previous.name:
        raise TemplateError("Changing the analytical method requires a new complete proposal")
    revised = TemplateCall.model_validate({**previous.model_dump(), **patch.model_dump(exclude_none=True)})
    build_template(revised.name, revised.params)
    problems = template_intent_problems(correction, revised, cat, data)
    if problems:
        raise TemplateError("; ".join(problems))
    return Decision("template", template=revised, restated="update the analysis as requested: " + correction)
