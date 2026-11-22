"""Store: memory."""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import (
    delete,
    insert,
    select,
    update,
)

from ..config import get_settings
from ..memory import Memory, _score, _tokens, fresh
from .models import _id, gap_resolutions, memories

if TYPE_CHECKING:
    from .core import Store

class MemoryStore:
    def memories(self, *, user_id: str | None = None, project_id: str | None = None,
                 scope: str | None = None, data_size: str | None = None) -> list[dict]:
        stmt = select(memories).where(memories.c.superseded_by.is_(None))
        if scope:
            stmt = stmt.where(memories.c.scope == scope)
        if user_id:
            stmt = stmt.where(memories.c.user_id == user_id)
        if project_id:
            stmt = stmt.where(memories.c.project_id == project_id)
        if data_size:
            stmt = stmt.where(memories.c.data_size == data_size)
        return self._all(stmt.order_by(memories.c.created_at.desc()).limit(500))

    def delete_memory(self, user_id: str, memory_id: str, is_admin: bool = False) -> None:
        cond = memories.c.id == memory_id
        if not is_admin:
            cond = cond & (memories.c.user_id == user_id) & (memories.c.scope != "business")
        self._exec(delete(memories).where(cond))

    def promote_memory(self, memory_id: str) -> None:
        """Admin: make an insight part of the shared business memory."""
        self._exec(update(memories).where(memories.c.id == memory_id).values(scope="business", project_id=None))

    def resolved_gaps(self) -> set[tuple[str, str]]:
        return {(r["gap_id"], r["window_end"]) for r in self._all(select(gap_resolutions))}

    def resolve_gap(self, gap_id: str, window_end: str, memory_id: str, user_id: str) -> None:
        if (gap_id, window_end) not in self.resolved_gaps():
            self._exec(insert(gap_resolutions).values(gap_id=gap_id, window_end=window_end, memory_id=memory_id,
                                                      user_id=user_id))


class MemoryScope:
    """Memory for one question: reads chat/project/user/business memory, writes the new insight."""

    def __init__(self, store: Store, user: dict, chat: dict, data_size: str | None = None):
        self.store, self.user, self.chat = store, user, chat
        self.data_size = data_size or get_settings().data_size
        self.business = Memory(data_size=self.data_size)              # profile + pulse gaps (files)
        self.project = store.project(user["id"], chat["project_id"]) if chat.get("project_id") else None

    def _related(self, rows: list[dict], question: str, k: int, recent: int = 0) -> list[dict]:
        """Best-matching memories; if fewer than `recent` match, top up with the newest ones."""
        docs = [_tokens(" ".join([r.get("question") or "", r.get("headline") or "",
                                  " ".join(f.get("statement", "") for f in (r.get("facts") or []))])) for r in rows]
        if not docs:
            return []
        from collections import Counter
        df = Counter(t for d in docs for t in set(d))
        q = _tokens(question)
        scored = sorted(((_score(q, d, df, len(docs)), r) for d, r in zip(docs, rows, strict=True)),
                        key=lambda x: (x[0], str(x[1]["created_at"])), reverse=True)
        picked = [r for s, r in scored if s > 0][:k]
        for r in rows:                                   # rows are newest first
            if len(picked) >= max(recent, len(picked)) or len(picked) >= k:
                break
            if r not in picked:
                picked.append(r)
        return picked

    @staticmethod
    def _lines(rows: list[dict]) -> str:
        out = []
        for r in rows:
            facts = "; ".join(f"[{f.get('type', 'fact')}] {f.get('statement')}" for f in (r.get("facts") or []))
            out.append(f"- (data as of {r['data_as_of']}) Q: {r['question']} -> {r['headline']}"
                       + (f" Facts: {facts}" if facts else ""))
        return "\n".join(out)

    def open_gaps(self, question: str | None = None, k: int = 4) -> list[dict]:
        resolved = self.store.resolved_gaps()
        return [g for g in self.business.open_gaps(question, k=20)
                if (g["id"], g.get("window_end")) not in resolved][:k]

    def constraints(self) -> str:
        """Standing instructions, separate from historical numerical observations."""
        return "\n".join(filter(None, [self.user.get("preferences"),
                         (self.project or {}).get("instructions"), self.business.profile()]))

    def context(self, question: str, as_of: str | None = None) -> str:
        parts = []
        if self.user.get("preferences"):
            parts.append("### About the user and their preferences\n" + self.user["preferences"].strip())
        if self.project:
            if self.project.get("instructions"):
                parts.append(f"### Project '{self.project['name']}' instructions\n"
                             + self.project["instructions"].strip())
            rows = fresh(self.store.memories(project_id=self.project["id"], scope="project",
                                             data_size=self.data_size), as_of)
            if related := self._related(rows, question, 4, recent=2):
                parts.append("### Earlier insights in this project\n" + self._lines(related))
        else:
            rows = fresh(self.store.memories(user_id=self.user["id"], scope="user", data_size=self.data_size), as_of)
            if related := self._related(rows, question, 3):
                parts.append("### The user's earlier insights\n" + self._lines(related))
        profile = self.business.profile()
        if profile:
            parts.append("### Business profile\n" + profile)
        business = fresh(self.store.memories(scope="business", data_size=self.data_size), as_of)
        if related := self._related(business, question, 3):
            parts.append("### Shared business knowledge\n" + self._lines(related))
        if gaps := self.open_gaps(question):
            parts.append("### Open gaps (unexplained KPI changes from the daily pulse)\n" + "\n".join(
                f"- id={g['id']}: {g.get('label')} ({g.get('breakdown')}={g.get('segment')}) "
                f"{g.get('change_pct'):+}% vs the previous 8 weeks, week ending {g.get('window_end')}" for g in gaps))
        return "\n\n".join(parts) or "(nothing remembered yet)"

    def save_insight(self, *, question: str, plan: dict, sql: str, analysis: dict, data_as_of: str,
                     chat_id: str | None = None) -> dict:
        scope = "project" if self.project else "user"
        mid = _id()
        values = dict(id=mid, scope=scope, user_id=self.user["id"],
                      project_id=self.project["id"] if self.project else None, chat_id=self.chat["id"],
                      question=question, headline=analysis.get("headline"), facts=analysis.get("memory", []),
                      sql=sql, metrics=plan.get("metrics", []), data_size=self.data_size, data_as_of=data_as_of)
        same = [m for m in self.store.memories(user_id=self.user["id"], scope=scope, data_size=self.data_size)
                if m.get("project_id") == values["project_id"] and _tokens(m["question"] or "") == _tokens(question)]
        with self.store.engine.begin() as c:
            c.execute(insert(memories).values(**values))
            for old in same:                          # a newer answer to the same question replaces the old one
                c.execute(update(memories).where(memories.c.id == old["id"]).values(superseded_by=mid))
        return {"id": mid, **values}

    def close_gaps(self, gap_ids: list[str], insight_id: str, as_of: str) -> int:
        gaps = {g["id"]: g for g in self.open_gaps(k=100)}
        closed = 0
        for gid in gap_ids:
            if gid in gaps:
                self.store.resolve_gap(gid, gaps[gid].get("window_end") or as_of, insight_id, self.user["id"])
                closed += 1
        return closed
