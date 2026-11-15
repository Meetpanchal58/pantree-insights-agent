"""Dimension-aware result scoring: is the agent's result the same answer as the gold result?

1. Find the agent column for each gold key (dimension) column: the column whose normalised values are
   the same set (dates compared as YYYY-MM-DD, months as YYYY-MM, text case-insensitive).
   No such column -> "missing dimension". Duplicate key rows -> "duplicate rows".
2. The set of key rows must be identical: missing or extra rows fail.
3. Rows are aligned on the keys; each gold metric column must match one agent numeric column on every
   aligned row within TOLERANCE (1% relative, or 1e-6 absolute). A ratio reported as a percentage
   (x100) is accepted. Values assigned to the wrong category fail because rows are aligned first.
Column names and extra columns do not matter.
"""
from __future__ import annotations

import pandas as pd

TOLERANCE = 0.01


def _norm(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "∅"
    if isinstance(value, pd.Timestamp):
        value = value.isoformat()
    text = str(value).strip().lower()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":          # ISO date / timestamp
        return text[:10]
    if text in ("true", "false"):
        return text
    try:
        number = float(text)
        return str(int(number)) if number.is_integer() else f"{number:.6g}"
    except ValueError:
        return text


def _close(a: float, b: float) -> bool:
    if pd.isna(a) and pd.isna(b):
        return True
    if pd.isna(a) or pd.isna(b):
        return False
    return abs(a - b) <= TOLERANCE * max(abs(a), abs(b)) or abs(a - b) < 1e-6


def _numeric(frame: pd.DataFrame) -> list[str]:
    return [c for c in frame.columns if pd.api.types.is_numeric_dtype(frame[c]) and frame[c].dtype != bool]


def score(gold: pd.DataFrame, agent: pd.DataFrame, key_columns: list[str],
          metric_columns: list[str]) -> tuple[bool, str]:
    ok, reason, _ = align(gold, agent, key_columns, metric_columns)
    return ok, reason


def align(gold: pd.DataFrame, agent: pd.DataFrame, key_columns: list[str],
          metric_columns: list[str]) -> tuple[bool, str, dict[str, str]]:
    """Like score(), plus the column mapping agent column -> gold column (keys and matched metrics)."""
    found_map: dict[str, str] = {}
    if gold.empty:                                    # the correct answer is "no rows"
        return (agent is None or agent.empty), ("match" if agent is None or agent.empty else "extra rows"), {}
    if agent is None or agent.empty:
        return False, "missing rows (empty result)", {}
    gold, agent = gold.reset_index(drop=True), agent.reset_index(drop=True)

    # 1. dimensions
    mapping: dict[str, str] = {}
    for key in key_columns:
        wanted = sorted(map(_norm, gold[key]))
        candidates = [key] + [c for c in agent.columns if c != key] if key in agent.columns else list(agent.columns)
        found = next((c for c in candidates if c not in mapping.values()
                      and sorted(map(_norm, agent[c])) == wanted), None)
        if found is None:
            if key not in agent.columns and not any(sorted(map(_norm, agent[c])) for c in agent.columns
                                                    if set(map(_norm, agent[c])) & set(wanted)):
                return False, f"wrong dimension: no column matches '{key}'", {}
            have = {v for c in agent.columns for v in map(_norm, agent[c])}
            missing = set(wanted) - have
            return False, ("missing rows" if missing else "extra rows") + f" for '{key}'", {}
        mapping[key] = found
    if key_columns:
        agent_keys = agent[list(mapping.values())].map(_norm).apply(tuple, axis=1)
        if agent_keys.duplicated().any():
            return False, "extra rows (duplicates for the same dimension values)", {}
        gold_keys = gold[key_columns].map(_norm).apply(tuple, axis=1)
        if set(agent_keys) != set(gold_keys):
            return False, ("missing rows" if set(agent_keys) < set(gold_keys) else "extra rows"), {}
        order = {k: i for i, k in enumerate(agent_keys)}
        agent = agent.iloc[[order[k] for k in gold_keys]].reset_index(drop=True)
    elif len(agent) != len(gold):
        return False, ("missing rows" if len(agent) < len(gold) else "extra rows") + \
            f" (expected {len(gold)}, got {len(agent)})", {}

    # 3. metrics on aligned rows
    numeric = _numeric(agent)
    for metric in metric_columns:
        expected = gold[metric].astype(float).tolist()
        options = ([metric] if metric in numeric else []) + [c for c in numeric if c != metric]
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
