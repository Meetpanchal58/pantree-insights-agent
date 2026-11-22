"""Grounding: every number in an answer is a structured claim, verified against a result table.

The analyst writes prose with placeholders ({c1}) and a list of claims:
    {"id": "c1", "value": 0.253, "unit": "ratio", "column": "return_rate",
     "row": [{"column": "category", "value": "Footwear"}], "op": "value"}
The verifier looks up the row by its key values, applies the operation and compares with a relative
tolerance (default 0.5%; display rounding to the claim's decimals is allowed). Any other digit sequence
in the prose fails, except dates, years and explicit top/bottom/rank labels (<= 12). The same verifier checks
claims against the agent's own result (in the app) and against the gold result (in evaluation).

Ops: value (one cell), diff (row - against), ratio (row / against), pct_change (row / against - 1),
share (row / column total), sum and mean (over the rows matching `row`; [] = all rows).
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

import pandas as pd
from pydantic import BaseModel, Field

REL_TOLERANCE = 0.005
Op = Literal["value", "diff", "ratio", "pct_change", "share", "sum", "mean", "count", "min", "max"]
Unit = Literal["ratio", "multiple", "count", "inr", "pct_points", "number", "days", "minutes", "rating"]


class KeyValue(BaseModel):
    column: str = Field(description="A key (dimension) column of the result")
    value: str = Field(description="Its value in the row, as shown in the result")


class Claim(BaseModel):
    id: str = Field(pattern=r"^c\d+$", description="c1, c2, ... referenced as {c1} in the text")
    value: float = Field(description="The number claimed, in the column's units (ratios as 0.253, not 25.3)")
    unit: Unit = Field(description="ratio (shown as %), pct_points, count, inr, number, days, minutes, rating")
    column: str = Field(description="Result column the number comes from")
    row: list[KeyValue] = Field(default_factory=list, description="Key values identifying the row; [] = all rows")
    op: Op = Field("value", description="value | diff | ratio | pct_change | share | sum | mean | count (rows, "
                   "or distinct values of column, matching row) | min | max")
    against: list[KeyValue] | None = Field(None, description="Comparison row for diff, ratio, pct_change")
    decimals: int = Field(1, ge=0, le=6, description="Decimals shown in the text (for display rounding)")


@dataclass
class Grounding:
    ok: bool
    problems: list[str] = field(default_factory=list)
    checked: int = 0
    claims: list[Claim] = field(default_factory=list)


# ----------------------------------------------------------------------------- rendering
def _lakh_crore(v: float) -> str:
    if abs(v) >= 1e7:
        return f"{v / 1e7:.2f} crore"
    if abs(v) >= 1e5:
        return f"{v / 1e5:.2f} lakh"
    return f"{v:,.0f}"


def display(claim: Claim) -> str:
    v, d = claim.value, claim.decimals
    if claim.unit == "ratio":
        return f"{v * 100:.{d}f}%"
    if claim.unit == "pct_points":
        return f"{v * 100:.{d}f} pp"
    if claim.unit == "multiple":
        return f"{v:.{d}f}×"
    if claim.unit == "inr":
        return _lakh_crore(v)
    if claim.unit == "count":
        return f"{v:,.0f}"
    return f"{v:,.{d}f}"


def render(text: str, claims: list[Claim]) -> str:
    """Replace {cN} placeholders with the claims' formatted values."""
    by_id = {c.id: c for c in claims}
    return re.sub(r"\{(c\d+)\}", lambda m: display(by_id[m.group(1)]) if m.group(1) in by_id else m.group(0), text)


# ----------------------------------------------------------------------------- free numbers in prose
_MONTHS = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DATES = re.compile(rf"\b\d{{4}}-\d{{2}}(-\d{{2}})?\b|\b{_MONTHS}\s+\d{{4}}\b"
                    rf"|\b{_MONTHS}\s+\d{{1,2}}(?!\d)(\s*[-–]\s*\d{{1,2}}(?!\d))?(,?\s+\d{{4}})?"
                    rf"|\b\d{{1,2}}(st|nd|rd|th)?(\s*[-–]\s*\d{{1,2}}(st|nd|rd|th)?)?\s+(of\s+)?{_MONTHS}(\s+\d{{4}})?"
                    r"|\bq[1-4]\b", re.I)
_NUMBER = re.compile(r"(?<![\w{])[-+]?\d[\d,]*(?:\.\d+)?%?(?![\w}])")


def free_numbers(text: str) -> list[str]:
    """Numbers typed directly into prose (not claims): allowed only for dates, years and ranks <= 12."""
    text = re.sub(r"\{c\d+\}", " ", text)
    text = _DATES.sub(" ", text)
    text = re.sub(r"\b(?:top|bottom|rank(?:ed)?)\s+(?:1[0-2]|[1-9])\b", " ", text, flags=re.I)
    bad = []
    for token in _NUMBER.findall(text):
        raw = token.rstrip("%").replace(",", "")
        try:
            number = float(raw)
        except ValueError:
            continue
        if not token.endswith("%") and number.is_integer() and 1990 <= number <= 2100:
            continue
        bad.append(token)
    return bad


# ----------------------------------------------------------------------------- verification
def _norm(value, numeric: bool = False) -> str:
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[T ]00:00:00(?:\.000)?(?:Z)?)?", text):
        return text[:10]
    if not numeric:
        return text
    try:
        number = float(text)
        return str(int(number)) if number.is_integer() else text
    except ValueError:
        return {"true": "true", "false": "false"}.get(text.lower(), text)


def _rows(frame: pd.DataFrame, keys: list[KeyValue] | None) -> pd.DataFrame:
    mask = pd.Series(True, index=frame.index)
    for kv in keys or []:
        if kv.column not in frame.columns:
            raise KeyError(f"unknown key column '{kv.column}'")
        numeric = pd.api.types.is_numeric_dtype(frame[kv.column])
        mask &= frame[kv.column].map(lambda v, numeric=numeric: _norm(v, numeric)) == _norm(kv.value, numeric)
    return frame[mask]


def _one(frame: pd.DataFrame, keys: list[KeyValue] | None, column: str) -> float:
    rows = _rows(frame, keys)
    if len(rows) != 1:
        raise KeyError(f"{len(rows)} rows match {[(k.column, k.value) for k in keys or []]}")
    return float(rows[column].iloc[0])


def actual(frame: pd.DataFrame, claim: Claim) -> float:
    if claim.column not in frame.columns:
        raise KeyError(f"unknown column '{claim.column}'")
    if claim.op == "value":
        return _one(frame, claim.row, claim.column)
    if claim.op == "sum":
        return float(_rows(frame, claim.row)[claim.column].sum(min_count=1))
    if claim.op == "mean":
        return float(_rows(frame, claim.row)[claim.column].mean())
    if claim.op == "count":                             # "13 SKUs are out of stock": distinct values in the rows
        return float(_rows(frame, claim.row)[claim.column].nunique())
    if claim.op in ("min", "max"):
        values = _rows(frame, claim.row)[claim.column]
        return float(values.min() if claim.op == "min" else values.max())
    if claim.op == "share":
        return _one(frame, claim.row, claim.column) / float(frame[claim.column].sum())
    if not claim.against:
        raise KeyError(f"op '{claim.op}' needs 'against'")
    a, b = _one(frame, claim.row, claim.column), _one(frame, claim.against, claim.column)
    if claim.op == "diff":
        return a - b
    return a / b if claim.op == "ratio" else a / b - 1


def _close(claim: Claim, real: float) -> bool:
    if abs(claim.value - real) <= REL_TOLERANCE * max(abs(real), 1e-12):
        return True
    shown = Claim(**{**claim.model_dump(), "value": real})          # display rounding: same text when shown
    return display(shown) == display(claim)


def unsupported_assertion(text: str) -> bool:
    """Reject causal language that an observational query cannot establish."""
    return bool(re.search(r"\b(caused?|causes|because|driven by|due to|resulted in|led to|proves?|"
                          r"attributable to|responsible for|as a result of|the reason for)\b", text, re.I))


def qualitative_problems(text: str, claims: list[Claim], frame: pd.DataFrame, truncated: bool) -> list[str]:
    """Conservative evidence mapping for explicit direction and superlative statements.

    This is a lexical safeguard, not a general semantic faithfulness score. Comparisons must be local to
    their sentence and carry a checked signed difference; ranks require a complete result population.
    """
    problems = []
    by_id = {c.id: c for c in claims}
    for sentence in re.split(r"[.!?;]\s+", text):
        # Match complete labels before their substrings: "Red" inside an observed product title
        # is not a separate mention of the brand Red. An explicitly named Red elsewhere still is.
        mentions = [(column, str(value).casefold(), match.start(), match.end())
                    for column in frame.columns if not pd.api.types.is_numeric_dtype(frame[column])
                    for value in frame[column].dropna().unique() if len(str(value)) >= 3
                    for match in re.finditer(rf"(?<!\w){re.escape(str(value))}(?!\w)", sentence, re.I)]
        mentions = [m for m in mentions if not any(
            other[2] <= m[2] and other[3] >= m[3] and other[3] - other[2] > m[3] - m[2]
            for other in mentions)]
        evidence = [by_id[i] for i in re.findall(r"\{(c\d+)\}", sentence) if i in by_id]
        for c in evidence:
            for key in [*c.row, *(c.against or [])]:
                if key.column not in frame or pd.api.types.is_numeric_dtype(frame[key.column]):
                    continue
                allowed = {k.value.casefold() for claim in evidence for k in [*claim.row, *(claim.against or [])]
                           if k.column == key.column}
                mentioned = {value for column, value, _, _ in mentions if column == key.column}
                if mentioned - allowed:
                    problems.append("the named entity or period does not match this claim's evidence row")
                # Bind each placeholder locally as well: two correct values must not swap their named rows.
                own = {k.value.casefold() for k in [*c.row, *(c.against or [])] if k.column == key.column}
                positions = [(start, value) for column, value, start, _ in mentions if column == key.column]
                for placeholder in re.finditer(re.escape("{" + c.id + "}"), sentence):
                    preceding = [(pos, value) for pos, value in positions if pos < placeholder.start()]
                    nearest = max(preceding, default=None) if preceding else min(positions, default=None)
                    if nearest and nearest[1] not in own:
                        problems.append("the entity nearest this claim differs from its evidence row")
        up = bool(re.search(r"\b(increased?|grew|rose|higher than)\b", sentence, re.I))
        down = bool(re.search(r"\b(decreased?|fell|declined?|lower than)\b", sentence, re.I))
        if up or down:
            comparisons = [c for c in evidence if c.op in ("diff", "pct_change")]
            if not comparisons or (up and down) or any((c.value <= 0 if up else c.value >= 0) for c in comparisons):
                problems.append("direction needs a matching signed comparison claim in the same sentence")
        high = bool(re.search(r"\b(highest|largest|maximum|leading)\b", sentence, re.I))
        low = bool(re.search(r"\b(lowest|smallest|minimum)\b", sentence, re.I))
        if high or low:
            supported = not truncated and not (high and low) and evidence
            for c in evidence:
                values = pd.to_numeric(frame[c.column], errors="coerce")
                supported = supported and c.op in ("value", "max" if high else "min") and \
                    c.value == (values.max() if high else values.min())
            if not supported:
                problems.append("rank needs an extremum claim and a complete result population")
    return problems


def verify(frame: pd.DataFrame, claims: list[Claim], texts: dict[str, str | list[str]], *,
           units: dict[str, str] | None = None, truncated: bool = False) -> Grounding:
    """Check every claim against the frame and every text field for free-typed numbers and unknown ids."""
    problems, ids = [], {c.id for c in claims}
    checked = []
    duplicates = {k for k, v in Counter(c.id for c in claims).items() if v > 1}
    for claim in claims:
        try:
            if claim.id in duplicates:
                raise ValueError("duplicate claim id")
            if not math.isfinite(claim.value):
                raise ValueError("claim must be finite")
            if truncated and claim.op != "value":
                raise ValueError("aggregate/comparison claims require the complete result")
            unit = (units or {}).get(claim.column)
            if unit in ("ratio", "multiple") and claim.op in ("sum", "mean"):
                raise ValueError("rates and multiples need their original numerators and denominators for aggregation")
            expected = "ratio" if claim.op in ("ratio", "pct_change", "share") else \
                "count" if claim.op == "count" else "pct_points" if claim.op == "diff" and unit == "ratio" else unit
            if expected and claim.unit != expected:
                raise ValueError(f"unit must be {expected}, not {claim.unit}")
            real = actual(frame, claim)
        except (KeyError, ZeroDivisionError, ValueError, TypeError) as error:
            problems.append(f"{claim.id} ({display(claim)}): {error}")
            continue
        if not math.isfinite(real) or not _close(claim, real):
            problems.append(f"{claim.id} ({display(claim)}): result has {real:.6g} for {claim.op} of {claim.column}")
        else:
            checked.append(claim.model_copy(update={"value": real}))
    for name, value in texts.items():
        for text in value if isinstance(value, list) else [value]:
            if unsupported_assertion(text or ""):
                problems.append(f"{name}: causal assertion is not established by this result")
            problems += [f"{name}: {p}" for p in qualitative_problems(text or "", checked, frame, truncated)]
            for missing in set(re.findall(r"\{(c\d+)\}", text or "")) - ids:
                problems.append(f"{name}: placeholder {{{missing}}} has no claim")
            for token in free_numbers(text or ""):
                problems.append(f"{name}: number '{token}' is not a claim")
    return Grounding(not problems, problems, len(claims), checked)
