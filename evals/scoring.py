"""Dimension-aware result scoring: is the agent's result the same answer as the gold result?

1. Find the agent column for each gold key (dimension) column: the column whose normalised values are
   the same set (dates compared as YYYY-MM-DD, months as YYYY-MM, text case preserved).
   No such column -> "missing dimension". Duplicate key rows -> "duplicate rows".
2. The set of key rows must be identical: missing or extra rows fail.
3. Rows are aligned on the keys; each gold metric column must match one agent numeric column on every
   aligned row within TOLERANCE (1% relative, or 1e-6 absolute). A ratio reported as a percentage
   (x100) is accepted. Values assigned to the wrong category fail because rows are aligned first.
Column names and extra columns do not matter.

Fairness rules (each unit-tested in tests/test_scoring.py):
- A gold key column the agent left out is optional when the gold rows are still unique on the keys it
  did return (e.g. category next to sku_id): it is descriptive, not a different breakdown.
- Computed labels match on their words ("Sale event" = "sale-event day"), one-to-one only.
- trim_top_n: when the gold is a ranked list (ORDER BY ... LIMIT n) and the question names no size, only
  the agent's first n rows are compared, so "top 50" vs "top 10" is not a wrong answer.
"""
from __future__ import annotations

import re

import pandas as pd

TOLERANCE = 0.01
_FILLER = {"day", "days", "the", "a", "an", "of", "and"}


def _norm(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "∅"
    if isinstance(value, pd.Timestamp):
        value = value.isoformat()
    text = str(value).strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":          # ISO date / timestamp
        return text[:10]
    if text.lower() in ("true", "false"):
        return text.lower()
    try:
        number = float(text)
        return str(int(number)) if number.is_integer() else f"{number:.6g}"
    except ValueError:
        return text


def _words(value) -> frozenset[str]:
    return frozenset(w for w in re.findall(r"[a-z0-9]+", _norm(value).lower()) if w not in _FILLER)


def _label_map(wanted: list[str], values: pd.Series, allow_case: bool = False) -> dict[str, str] | None:
    """agent value -> gold value when the labels are the same words, one-to-one; else None."""
    gold_words = {w: v for v in set(wanted) if (w := _words(v))}
    if len(gold_words) != len(set(wanted)):
        return None
    out = {}
    for value in set(map(_norm, values)):
        match = gold_words.get(_words(value))
        # Case-only changes can identify different stored entities (ADIDAS versus Adidas).
        if match is None or (not allow_case and _norm(value) != match and _norm(value).casefold() == match.casefold()) \
                or match in out.values():
            return None
        out[value] = match
    return out if sorted(out.values()) == sorted(set(wanted)) else None


_NEGATION = re.compile(r"\b(non|not|no|without|never|false)\b", re.I)


def _boolean_map(wanted: list[str], values: pd.Series) -> dict[str, str] | None:
    """agent label -> 'true'/'false' when the gold key is boolean and the agent wrote two readable labels,
    exactly one of them negated ('Prime' / 'Non-Prime'); else None."""
    if set(wanted) - {"true", "false"} or pd.api.types.is_numeric_dtype(values):
        return None
    labels = sorted(set(map(_norm, values)))
    if len(labels) != 2:
        return None
    negated = [lab for lab in labels if _NEGATION.search(lab.replace("-", " "))]
    if len(negated) != 1:
        return None
    return {lab: ("false" if lab in negated else "true") for lab in labels}


_NUMBER_IN_QUESTION = re.compile(r"\b(top|bottom|first|last|best|worst)\s+\d+|"
                                 r"\b\d+\s+(best|worst|top|largest|biggest)", re.I)
_RANKED = re.compile(r"order\s+by[\s\S]+limit\s+(\d+)\s*;?\s*$", re.I)


def trim_top_n(agent: pd.DataFrame, gold: pd.DataFrame, gold_sql: str, question: str) -> pd.DataFrame:
    """Compare only the agent's first len(gold) rows when the gold is a ranked LIMIT and no size was asked."""
    if agent is None or gold is None or len(agent) <= len(gold) or not _RANKED.search(gold_sql.strip()):
        return agent
    if _NUMBER_IN_QUESTION.search(question):
        return agent
    return agent.head(len(gold))


def _close(a: float, b: float) -> bool:
    if pd.isna(a) and pd.isna(b):
        return True
    if pd.isna(a) or pd.isna(b):
        return False
    return abs(a - b) <= TOLERANCE * max(abs(a), abs(b)) or abs(a - b) < 1e-6


def _numeric(frame: pd.DataFrame) -> list[str]:
    return [c for c in frame.columns if pd.api.types.is_numeric_dtype(frame[c]) and frame[c].dtype != bool]


def score(gold: pd.DataFrame, agent: pd.DataFrame, key_columns: list[str],
          metric_columns: list[str], *, label_columns: set[str] = frozenset()) -> tuple[bool, str]:
    ok, reason, _ = align(gold, agent, key_columns, metric_columns, label_columns=label_columns)
    return ok, reason


def align(gold: pd.DataFrame, agent: pd.DataFrame, key_columns: list[str],
          metric_columns: list[str], *, label_columns: set[str] = frozenset(),
          superset: bool = False) -> tuple[bool, str, dict[str, str]]:
    """Like score(), plus the column mapping agent column -> gold column (keys and matched metrics).
    `superset`: the agent may return more rows than the gold (a full list where the gold kept its top N); every
    gold row must be there with the same numbers."""
    found_map: dict[str, str] = {}
    if gold.empty:                                    # the correct answer is "no rows"
        return (agent is None or agent.empty), ("match" if agent is None or agent.empty else "extra rows"), {}
    if agent is None or agent.empty:
        return False, "missing rows (empty result)", {}
    gold, agent = gold.reset_index(drop=True), agent.reset_index(drop=True)

    # 1. dimensions
    mapping: dict[str, str] = {}
    dropped: list[tuple[str, str]] = []
    agent = agent.copy()
    for key in key_columns:
        wanted = sorted(map(_norm, gold[key]))
        candidates = [key] + [c for c in agent.columns if c != key] if key in agent.columns else list(agent.columns)
        found = next((c for c in candidates if c not in mapping.values()
                      and (sorted(map(_norm, agent[c])) == wanted
                           or (superset and set(wanted) <= set(map(_norm, agent[c]))))), None)
        if found is None:                              # computed labels with the same words, or true/false labels
            for c in candidates:
                if c in mapping.values() or pd.api.types.is_numeric_dtype(agent[c]):
                    continue
                labels = _label_map(wanted, agent[c], allow_case=key in label_columns) or _boolean_map(wanted, agent[c])
                if labels is not None and sorted(labels[_norm(v)] for v in agent[c]) == wanted:
                    agent[c] = [labels[_norm(v)] for v in agent[c]]
                    found = c
                    break
        if found is None:
            if key not in agent.columns and not any(sorted(map(_norm, agent[c])) for c in agent.columns
                                                    if set(map(_norm, agent[c])) & set(wanted)):
                dropped.append((key, f"wrong dimension: no column matches '{key}'"))
                continue
            have = {v for c in agent.columns for v in map(_norm, agent[c])}
            missing = set(wanted) - have
            dropped.append((key, ("missing rows" if missing else "extra rows") + f" for '{key}'"))
            continue
        mapping[key] = found
    # a left-out key is optional only if the other keys identify every gold row
    if dropped and (not mapping or gold[list(mapping)].map(_norm).apply(tuple, axis=1).duplicated().any()):
        return False, dropped[0][1], {}
    keys = list(mapping)
    if keys and superset:                              # keep the agent rows the gold has
        gold_set = set(gold[keys].map(_norm).apply(tuple, axis=1))
        rows = agent[list(mapping.values())].map(_norm).apply(tuple, axis=1).isin(gold_set)
        agent = agent[rows].reset_index(drop=True)
    if keys:
        agent_keys = agent[list(mapping.values())].map(_norm).apply(tuple, axis=1)
        if agent_keys.duplicated().any():
            return False, "extra rows (duplicates for the same dimension values)", {}
        gold_keys = gold[keys].map(_norm).apply(tuple, axis=1)
        if set(agent_keys) != set(gold_keys):
            return False, ("missing rows" if set(agent_keys) < set(gold_keys) else "extra rows"), {}
        order = {k: i for i, k in enumerate(agent_keys)}
        agent = agent.iloc[[order[k] for k in gold_keys]].reset_index(drop=True)
    elif key_columns:
        return False, dropped[0][1], {}
    elif len(agent) != len(gold):
        return False, ("missing rows" if len(agent) < len(gold) else "extra rows") + \
            f" (expected {len(gold)}, got {len(agent)})", {}

    # 3. metrics on aligned rows
    numeric = _numeric(agent)
    for metric in metric_columns:
        expected = gold[metric].astype(float).tolist()
        if metric in agent.columns and metric not in numeric:
            return False, f"wrong metric value: '{metric}' is not numeric", {}
        options = [metric] if metric in numeric else [c for c in numeric if c not in found_map]
        ok = False
        for col in options:
            got = agent[col].astype(float).tolist()
            if all(_close(e, g) for e, g in zip(expected, got, strict=True)) or \
                    all(_close(e * 100, g) for e, g in zip(expected, got, strict=True)):
                ok = True
                found_map[col] = metric
                break
        if not ok:
            return False, f"wrong metric value: '{metric}' does not match (value, denominator or category)", {}
    return True, "match", {**{v: k for k, v in mapping.items()}, **found_map}


def infer_columns(gold: pd.DataFrame) -> tuple[list[str], list[str]]:
    """For regression items without annotations: keys = non-numeric columns, metric = the main metric."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from pantree_insights.charts import main_metric

    numeric = _numeric(gold)
    keys = [c for c in gold.columns if c not in numeric]
    keys += [c for c in numeric if c in ("year", "position", "month")]
    metric = main_metric([c for c in numeric if c not in keys])
    return keys, [metric] if metric else []
