"""The app's database: users, projects, chats, messages, memory, sign-in codes and daily usage.

SQLite locally (.cache/app.db), Postgres (Neon) in production: the same SQLAlchemy Core code runs on both.

Memory has four levels, combined for every question by MemoryScope:
    chat      the conversation so far (follow-up questions work)
    project   the project's instructions + insights from all its chats
    user      the user's preferences + insights from chats outside projects
    business  shared knowledge: business profile, business facts (admins only) and the pulse's open gaps
"""
from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    delete,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Engine

from .config import get_settings
from .memory import Memory, _score, _tokens, fresh

metadata = MetaData()


def _id() -> str:
    return uuid.uuid4().hex[:16]


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


users = Table(
    "users", metadata,
    Column("id", String(16), primary_key=True, default=_id),
    Column("email", String(320), unique=True, nullable=False),
    Column("name", String(200)), Column("picture", String(500)),
    Column("auth_provider", String(20)), Column("is_admin", Boolean, default=False),
    Column("preferences", Text, default=""),                  # user-level memory, written by the user
    Column("created_at", DateTime, default=_now), Column("last_login_at", DateTime, default=_now),
)
projects = Table(
    "projects", metadata,
    Column("id", String(16), primary_key=True, default=_id),
    Column("user_id", String(16), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False),
    Column("name", String(200), nullable=False), Column("instructions", Text, default=""),
    Column("created_at", DateTime, default=_now), Column("updated_at", DateTime, default=_now),
)
chats = Table(
    "chats", metadata,
    Column("id", String(16), primary_key=True, default=_id),
    Column("user_id", String(16), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False),
    Column("project_id", String(16), ForeignKey("projects.id", ondelete="SET NULL"), index=True),
    Column("title", String(200), default="New chat"),
    Column("created_at", DateTime, default=_now), Column("updated_at", DateTime, default=_now),
)
messages = Table(
    "messages", metadata,
    Column("id", String(16), primary_key=True, default=_id),
    Column("chat_id", String(16), ForeignKey("chats.id", ondelete="CASCADE"), index=True, nullable=False),
    Column("role", String(10), nullable=False),               # user | assistant
    Column("content", Text, default=""),                      # question, or the answer's headline
    Column("run", JSON),                                      # final result + events, for the UI and replays
    Column("created_at", DateTime, default=_now),
)
memories = Table(
    "memories", metadata,
    Column("id", String(16), primary_key=True, default=_id),
    Column("scope", String(10), nullable=False, index=True),  # user | project | business
    Column("user_id", String(16), ForeignKey("users.id", ondelete="CASCADE"), index=True),
    Column("project_id", String(16), ForeignKey("projects.id", ondelete="CASCADE"), index=True),
    Column("chat_id", String(16)),
    Column("question", Text), Column("headline", Text),
    Column("facts", JSON),                                    # [{"statement", "type"}]
    Column("sql", Text), Column("metrics", JSON),
    Column("data_size", String(10)), Column("data_as_of", String(10)),
    Column("superseded_by", String(16)), Column("created_at", DateTime, default=_now),
)
gap_resolutions = Table(
    "gap_resolutions", metadata,
    Column("gap_id", String(200), primary_key=True), Column("window_end", String(10), primary_key=True),
    Column("memory_id", String(16)), Column("user_id", String(16)), Column("created_at", DateTime, default=_now),
)
email_codes = Table(
    "email_codes", metadata,
    Column("email", String(320), primary_key=True), Column("code_hash", String(64), nullable=False),
    Column("expires_at", DateTime, nullable=False), Column("attempts", Integer, default=0),
    Column("sent_at", DateTime, default=_now),
)
usage = Table(
    "usage", metadata,
    Column("user_id", String(16), primary_key=True), Column("day", String(10), primary_key=True),
    Column("questions", Integer, default=0), Column("usd", Float, default=0.0),
)


class Store:
    def __init__(self, url: str | None = None):
        url = url or get_settings().database_url
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        if url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        if url.startswith("sqlite:///"):
            from pathlib import Path
            Path(url[len("sqlite:///"):]).parent.mkdir(parents=True, exist_ok=True)
        self.engine: Engine = create_engine(url, pool_pre_ping=True, future=True)
        metadata.create_all(self.engine)

    def _one(self, stmt) -> dict | None:
        with self.engine.connect() as c:
            row = c.execute(stmt).mappings().first()
            return dict(row) if row else None

    def _all(self, stmt) -> list[dict]:
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(stmt).mappings().all()]

    def _exec(self, stmt) -> None:
        with self.engine.begin() as c:
            c.execute(stmt)

    # ------------------------------------------------------------------ users
    def upsert_user(self, email: str, name: str | None = None, picture: str | None = None,
                    provider: str = "email") -> dict:
        email = email.strip().lower()
        is_admin = email in {e.strip().lower() for e in get_settings().admin_emails}
        existing = self._one(select(users).where(users.c.email == email))
        if existing:
            self._exec(update(users).where(users.c.id == existing["id"]).values(
                last_login_at=_now(), is_admin=is_admin, name=name or existing["name"],
                picture=picture or existing["picture"]))
        else:
            self._exec(insert(users).values(id=_id(), email=email, name=name or email.split("@")[0],
                                            picture=picture, auth_provider=provider, is_admin=is_admin))
        return self._one(select(users).where(users.c.email == email))

    def user(self, user_id: str) -> dict | None:
        return self._one(select(users).where(users.c.id == user_id))

    def set_preferences(self, user_id: str, text: str) -> None:
        self._exec(update(users).where(users.c.id == user_id).values(preferences=text[:4000]))

    # ------------------------------------------------------------------ email codes
    def new_email_code(self, email: str, minutes: int = 10) -> str | None:
        """A 6-digit code, or None if one was sent less than 60 seconds ago (rate limit)."""
        email = email.strip().lower()
        existing = self._one(select(email_codes).where(email_codes.c.email == email))
        if existing and existing["sent_at"] > _now() - timedelta(seconds=60):
            return None
        code = f"{secrets.randbelow(10**6):06d}"
        values = {"code_hash": hashlib.sha256(f"{email}:{code}".encode()).hexdigest(),
                  "expires_at": _now() + timedelta(minutes=minutes), "attempts": 0, "sent_at": _now()}
        with self.engine.begin() as c:
            c.execute(delete(email_codes).where(email_codes.c.email == email))
            c.execute(insert(email_codes).values(email=email, **values))
        return code

    def check_email_code(self, email: str, code: str) -> bool:
        email = email.strip().lower()
        row = self._one(select(email_codes).where(email_codes.c.email == email))
        if not row or row["expires_at"] < _now() or row["attempts"] >= 5:
            return False
        ok = secrets.compare_digest(row["code_hash"], hashlib.sha256(f"{email}:{code.strip()}".encode()).hexdigest())
        if ok:
            self._exec(delete(email_codes).where(email_codes.c.email == email))
        else:
            self._exec(update(email_codes).where(email_codes.c.email == email).values(attempts=row["attempts"] + 1))
        return ok

    # ------------------------------------------------------------------ usage
    def questions_today(self, user_id: str) -> int:
        row = self._one(select(usage).where(usage.c.user_id == user_id, usage.c.day == str(date.today())))
        return row["questions"] if row else 0

    def count_question(self, user_id: str, usd: float = 0.0) -> None:
        day = str(date.today())
        row = self._one(select(usage).where(usage.c.user_id == user_id, usage.c.day == day))
        if row:
            self._exec(update(usage).where(usage.c.user_id == user_id, usage.c.day == day).values(
                questions=row["questions"] + 1, usd=row["usd"] + usd))
        else:
            self._exec(insert(usage).values(user_id=user_id, day=day, questions=1, usd=usd))

    def usd_today(self, key: str) -> float:
        row = self._one(select(usage).where(usage.c.user_id == key, usage.c.day == str(date.today())))
        return float(row["usd"]) if row else 0.0

    def add_usd_today(self, key: str, usd: float) -> None:
        """Add spend for today under a key (atomic update; the row is created on first use)."""
        day = str(date.today())
        with self.engine.begin() as c:
            done = c.execute(update(usage).where(usage.c.user_id == key, usage.c.day == day)
                             .values(usd=usage.c.usd + usd)).rowcount
            if not done:
                c.execute(insert(usage).values(user_id=key, day=day, questions=0, usd=usd))

    def add_cost(self, user_id: str, usd: float) -> None:
        day = str(date.today())
        self._exec(update(usage).where(usage.c.user_id == user_id, usage.c.day == day).values(usd=usage.c.usd + usd))

    # ------------------------------------------------------------------ projects
    def projects(self, user_id: str) -> list[dict]:
        return self._all(select(projects).where(projects.c.user_id == user_id).order_by(projects.c.updated_at.desc()))

    def project(self, user_id: str, project_id: str) -> dict | None:
        return self._one(select(projects).where(projects.c.id == project_id, projects.c.user_id == user_id))

    def create_project(self, user_id: str, name: str, instructions: str = "") -> dict:
        pid = _id()
        self._exec(insert(projects).values(id=pid, user_id=user_id, name=name[:200], instructions=instructions[:4000]))
        return self.project(user_id, pid)

    def update_project(self, user_id: str, project_id: str, **values: Any) -> dict | None:
        values = {k: v for k, v in values.items() if k in ("name", "instructions") and v is not None}
        if values:
            self._exec(update(projects).where(projects.c.id == project_id, projects.c.user_id == user_id)
                       .values(**values, updated_at=_now()))
        return self.project(user_id, project_id)

    def delete_project(self, user_id: str, project_id: str) -> None:
        with self.engine.begin() as c:   # chats stay (moved out of the project); project memory goes
            c.execute(update(chats).where(chats.c.project_id == project_id, chats.c.user_id == user_id)
                      .values(project_id=None))
            c.execute(delete(memories).where(memories.c.project_id == project_id, memories.c.user_id == user_id))
            c.execute(delete(projects).where(projects.c.id == project_id, projects.c.user_id == user_id))

    # ------------------------------------------------------------------ chats and messages
    def chats(self, user_id: str, project_id: str | None = None) -> list[dict]:
        stmt = select(chats).where(chats.c.user_id == user_id)
        if project_id:
            stmt = stmt.where(chats.c.project_id == project_id)
        return self._all(stmt.order_by(chats.c.updated_at.desc()).limit(200))

    def chat(self, user_id: str, chat_id: str) -> dict | None:
        return self._one(select(chats).where(chats.c.id == chat_id, chats.c.user_id == user_id))

    def create_chat(self, user_id: str, project_id: str | None = None, title: str = "New chat") -> dict:
        if project_id and not self.project(user_id, project_id):
            project_id = None
        cid = _id()
        self._exec(insert(chats).values(id=cid, user_id=user_id, project_id=project_id, title=title[:200]))
        return self.chat(user_id, cid)

    def update_chat(self, user_id: str, chat_id: str, title: str | None = None,
                    project_id: str | None | bool = False) -> dict | None:
        values: dict[str, Any] = {"updated_at": _now()}
        if title:
            values["title"] = title[:200]
        if project_id is not False:                       # move into / out of a project
            values["project_id"] = project_id if project_id and self.project(user_id, project_id) else None
        self._exec(update(chats).where(chats.c.id == chat_id, chats.c.user_id == user_id).values(**values))
        return self.chat(user_id, chat_id)

    def delete_chat(self, user_id: str, chat_id: str) -> None:
        if self.chat(user_id, chat_id):
            with self.engine.begin() as c:
                c.execute(delete(messages).where(messages.c.chat_id == chat_id))
                c.execute(delete(chats).where(chats.c.id == chat_id))

    def messages(self, chat_id: str) -> list[dict]:
        return self._all(select(messages).where(messages.c.chat_id == chat_id).order_by(messages.c.created_at))

    def add_message(self, chat_id: str, role: str, content: str, run: dict | None = None) -> str:
        mid = _id()
        with self.engine.begin() as c:
            c.execute(insert(messages).values(id=mid, chat_id=chat_id, role=role, content=content, run=run))
            c.execute(update(chats).where(chats.c.id == chat_id).values(updated_at=_now()))
        return mid

    def history(self, chat_id: str, turns: int = 4) -> str:
        """The last few question/answer pairs, as text for the planner (follow-ups)."""
        rows = self.messages(chat_id)[-turns * 2:]
        lines = []
        for m in rows:
            if m["role"] == "user":
                lines.append(f"User: {m['content']}")
            else:
                run = m.get("run") or {}
                plan = (run.get("plan") or {}).get("restated_question")
                lines.append(f"Analyst: {m['content']}" + (f" (analysed: {plan})" if plan else ""))
        return "\n".join(lines)

    # ------------------------------------------------------------------ memory
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


@lru_cache
def get_store() -> Store:
    return Store()


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


def to_json(value: Any) -> Any:
    """Make DB rows JSON-friendly (datetimes as ISO strings)."""
    return json.loads(json.dumps(value, default=lambda v: v.isoformat() if hasattr(v, "isoformat") else str(v)))
