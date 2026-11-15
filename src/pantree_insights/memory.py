"""The agent's memory: insights it produced, the business profile and open gaps from the pulse.

    memory/insights/<date>_<slug>.json   one file per answered question (facts carry their numbers,
                                         SQL and data date, so old knowledge is never mistaken for new)
    memory/business_profile.md           running summary of the business (rewritten from insights)
    memory/gaps/open.json, resolved.json changes the pulse could not explain, and how they were closed

Numbers in memory always come from query results (the analyst may only use result numbers, and the
number check enforces it). Facts are typed: "fact" (measured) or "hypothesis" (a possible explanation).
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import get_settings

STOP = set(
    "a an and are as at be by did do does for from has have how in is it of on or our per show that the their this"
    " to vs was we what when which who why with than then over about into last year month week day days months"
    " weeks years me tell give".split()
)


def fresh(rows: list[dict], as_of: str | None) -> list[dict]:
    """Leave out insights whose data is more than memory_max_age_days older than the current data."""
    if not as_of:
        return rows
    cutoff = str(date.fromisoformat(as_of) - timedelta(days=get_settings().memory_max_age_days))
    return [r for r in rows if (r.get("data_as_of") or "") >= cutoff]


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 1 and t not in STOP]


def _score(query: list[str], doc: list[str], df: Counter, n_docs: int) -> float:
    """Small BM25-like overlap score (memory holds tens to hundreds of entries; no index needed)."""
    counts = Counter(doc)
    score = 0.0
    for term in set(query):
        if term in counts:
            idf = math.log(1 + (n_docs - df[term] + 0.5) / (df[term] + 0.5))
            score += idf * (counts[term] * 2.2) / (counts[term] + 1.2 * (0.25 + 0.75 * len(doc) / 40))
    return score


class Memory:
    def __init__(self, folder: Path | None = None, data_size: str | None = None):
        settings = get_settings()
        self.folder = folder or settings.memory_dir
        self.data_size = data_size or settings.data_size

    # ------------------------------------------------------------------ insights
    def insights(self) -> list[dict[str, Any]]:
        out = []
        for path in sorted((self.folder / "insights").glob("*.json")):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if item.get("data_size") == self.data_size and not item.get("superseded_by"):
                out.append(item)
        return out

    def save_insight(self, *, question: str, plan: dict, sql: str, analysis: dict, data_as_of: str,
                     chat_id: str | None = None) -> dict[str, Any]:
        slug = "-".join(_tokens(question)[:6]) or "insight"
        now = datetime.now()
        insight_id = f"{now:%Y-%m-%d_%H%M%S}_{slug}"[:80]
        item = {
            "id": insight_id, "created_at": now.isoformat(timespec="seconds"), "chat_id": chat_id,
            "question": question, "headline": analysis.get("headline"), "findings": analysis.get("findings", []),
            "facts": analysis.get("memory", []), "metrics": plan.get("metrics", []),
            "breakdowns": plan.get("breakdowns", []), "time_window": plan.get("time_window"), "sql": sql,
            "data_size": self.data_size, "data_as_of": data_as_of, "superseded_by": None,
        }
        # A newer answer to (almost) the same question replaces the older one, which is kept for history.
        for old in self.insights():
            if _tokens(old["question"]) == _tokens(question):
                old["superseded_by"] = insight_id
                self._write(old)
        self._write(item)
        return item

    def _write(self, item: dict) -> None:
        path = self.folder / "insights" / f"{item['id']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(item, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def related(self, question: str, k: int = 5) -> list[dict[str, Any]]:
        items = self.insights()
        if not items:
            return []
        docs = [_tokens(" ".join([i["question"], i.get("headline") or "", " ".join(i.get("metrics", [])),
                                  " ".join(f.get("statement", "") for f in i.get("facts", []))])) for i in items]
        df = Counter(t for d in docs for t in set(d))
        q = _tokens(question)
        scored = [(_score(q, d, df, len(docs)), i) for d, i in zip(docs, items, strict=True)]
        ranked = sorted(scored, key=lambda x: (x[0], x[1]["created_at"]), reverse=True)   # best, then newest
        return [i for s, i in ranked if s > 0][:k]

    # ------------------------------------------------------------------ gaps
    def open_gaps(self, question: str | None = None, k: int = 5) -> list[dict[str, Any]]:
        path = self.folder / "gaps" / "open.json"
        try:
            gaps = [g for g in json.loads(path.read_text()) if g.get("data_size") == self.data_size]
        except (OSError, ValueError):
            return []
        if question is None:
            return gaps[:k]
        docs = [_tokens(f"{g.get('label', '')} {g.get('kpi', '')} {g.get('breakdown', '')} {g.get('segment', '')}")
                for g in gaps]
        df = Counter(t for d in docs for t in set(d))
        q = _tokens(question)
        scored = sorted(((_score(q, d, df, max(len(docs), 1)), g) for d, g in zip(docs, gaps, strict=True)),
                        key=lambda x: -x[0])
        relevant = [g for s, g in scored if s > 0]
        return (relevant or gaps)[:k]

    def close_gaps(self, gap_ids: list[str], insight_id: str, as_of: str) -> int:
        open_path, resolved_path = self.folder / "gaps" / "open.json", self.folder / "gaps" / "resolved.json"
        try:
            gaps = json.loads(open_path.read_text())
            resolved = json.loads(resolved_path.read_text()) if resolved_path.exists() else []
        except (OSError, ValueError):
            return 0
        keep, closed = [], 0
        for gap in gaps:
            if gap.get("id") in gap_ids and gap.get("data_size") == self.data_size:
                gap.update(status="resolved", resolved_on=as_of, resolution=f"explained by insight {insight_id}")
                resolved.append(gap)
                closed += 1
            else:
                keep.append(gap)
        if closed:
            open_path.write_text(json.dumps(keep, indent=2, ensure_ascii=False) + "\n")
            resolved_path.write_text(json.dumps(resolved, indent=2, ensure_ascii=False) + "\n")
        return closed

    # ------------------------------------------------------------------ for prompts
    def profile(self) -> str:
        path = self.folder / "business_profile.md"
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        return "" if "Empty until" in text else text.strip()

    def context(self, question: str, as_of: str | None = None) -> str:
        """Memory as prompt text: profile, related insights (with their age) and relevant open gaps."""
        parts = []
        profile = self.profile()
        if profile:
            parts.append("### Business profile\n" + profile)
        related = fresh(self.related(question, k=10), as_of)[:5]
        if related:
            lines = []
            for item in related:
                facts = "; ".join(f"[{f.get('type', 'fact')}] {f.get('statement')}" for f in item.get("facts", []))
                lines.append(f"- (data as of {item['data_as_of']}) Q: {item['question']} -> {item.get('headline')}"
                             + (f" Facts: {facts}" if facts else ""))
            parts.append("### Earlier insights (background; re-check numbers, data may have changed)\n"
                         + "\n".join(lines))
        gaps = self.open_gaps(question, k=4)
        if gaps:
            parts.append("### Open gaps (unexplained KPI changes from the daily pulse)\n" + "\n".join(
                f"- id={g['id']}: {g.get('label')} ({g.get('breakdown')}={g.get('segment')}) "
                f"{g.get('change_pct'):+}% vs the previous 8 weeks, week ending {g.get('window_end')}"
                for g in gaps))
        return "\n\n".join(parts) or "(nothing remembered yet)"
