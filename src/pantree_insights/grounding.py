"""Grounding: every number in an answer is a structured claim, verified against a result table.

The analyst writes prose with placeholders ({c1}) and a list of claims:
    {"id": "c1", "value": 0.253, "unit": "ratio", "column": "return_rate",
     "row": [{"column": "category", "value": "Footwear"}], "op": "value"}
The verifier looks up the row by its key values, applies the operation and compares with a relative
tolerance (default 0.5%; display rounding to the claim's decimals is allowed). Any other digit sequence
in the prose fails, except dates, years and small ranks (<= 12). The same verifier checks claims against
the agent's own result (in the app) and against the gold result (in evaluation).

Ops: value (one cell), diff (row - against), ratio (row / against), pct_change (row / against - 1),
share (row / column total), sum and mean (over the rows matching `row`; [] = all rows).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

import pandas as pd
from pydantic import BaseModel, Field

REL_TOLERANCE = 0.005
Op = Literal["value", "diff", "ratio", "pct_change", "share", "sum", "mean"]
Unit = Literal["ratio", "count", "inr", "pct_points", "number", "days", "minutes", "rating"]


class KeyValue(BaseModel):
    column: str = Field(description="A key (dimension) column of the result")
    value: str = Field(description="Its value in the row, as shown in the result")


class Claim(BaseModel):
    id: str = Field(description="c1, c2, ... referenced as {c1} in the text")
    value: float = Field(description="The number claimed, in the column's units (ratios as 0.253, not 25.3)")
    unit: Unit = Field(description="ratio (shown as %), pct_points, count, inr, number, days, minutes, rating")
    column: str = Field(description="Result column the number comes from")
    row: list[KeyValue] = Field(default_factory=list, description="Key values identifying the row; [] = all rows")
    op: Op = Field("value", description="value | diff | ratio | pct_change | share | sum | mean")
    against: list[KeyValue] | None = Field(None, description="Comparison row for diff, ratio, pct_change")
    decimals: int = Field(1, description="Decimals shown in the text (for display rounding)")


@dataclass
class Grounding:
    ok: bool
    problems: list[str] = field(default_factory=list)
    checked: int = 0


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
    bad = []
    for token in _NUMBER.findall(text):
        raw = token.rstrip("%").replace(",", "")
        try:
            number = float(raw)
        except ValueError:
            continue
        if not token.endswith("%") and number.is_integer() and (0 <= number <= 12 or 1990 <= number <= 2100):
            continue
        bad.append(token)
    return bad


# ----------------------------------------------------------------------------- verification
def _norm(value) -> str:
    text = str(value).strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    try:
        number = float(text)
        return str(int(number)) if number.is_integer() else f"{number:.6g}"
    except ValueError:
        return {"true": "true", "false": "false"}.get(text.lower(), text)


def _rows(frame: pd.DataFrame, keys: list[KeyValue] | None) -> pd.DataFrame:
    mask = pd.Series(True, index=frame.index)
    for kv in keys or []:
        if kv.column not in frame.columns:
            raise KeyError(f"unknown key column '{kv.column}'")
        mask &= frame[kv.column].map(_norm) == _norm(kv.value)
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
        return float(_rows(frame, claim.row)[claim.column].sum())
    if claim.op == "mean":
        return float(_rows(frame, claim.row)[claim.column].mean())
    if claim.op == "share":
        return _one(frame, claim.row, claim.column) / float(frame[claim.column].sum())
    if not claim.against:
        raise KeyError(f"op '{claim.op}' needs 'against'")
    a, b = _one(frame, claim.row, claim.column), _one(frame, claim.against, claim.column)
    return {"diff": a - b, "ratio": a / b, "pct_change": a / b - 1}[claim.op]


def _close(claim: Claim, real: float) -> bool:
    if abs(claim.value - real) <= REL_TOLERANCE * max(abs(real), 1e-12):
        return True
    shown = Claim(**{**claim.model_dump(), "value": real})          # display rounding: same text when shown
    return display(shown) == display(claim)


def verify(frame: pd.DataFrame, claims: list[Claim], texts: dict[str, str | list[str]]) -> Grounding:
    """Check every claim against the frame and every text field for free-typed numbers and unknown ids."""
    problems, ids = [], {c.id for c in claims}
    for claim in claims:
        try:
            real = actual(frame, claim)
        except (KeyError, ZeroDivisionError, ValueError, TypeError) as error:
            problems.append(f"{claim.id} ({display(claim)}): {error}")
            continue
        if pd.isna(real) or not _close(claim, real):
            problems.append(f"{claim.id} ({display(claim)}): result has {real:.6g} for {claim.op} of {claim.column}")
    for name, value in texts.items():
        for text in value if isinstance(value, list) else [value]:
            for missing in set(re.findall(r"\{(c\d+)\}", text or "")) - ids:
                problems.append(f"{name}: placeholder {{{missing}}} has no claim")
            for token in free_numbers(text or ""):
                problems.append(f"{name}: number '{token}' is not a claim")
    return Grounding(not problems, problems, len(claims))
