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
import math
import secrets
import uuid
from datetime import UTC, datetime, timedelta
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
investigations = Table(
    "investigations", metadata,
    Column("chat_id", String(16), ForeignKey("chats.id", ondelete="CASCADE"), primary_key=True),
    Column("context", JSON, nullable=False), Column("updated_at", DateTime, default=_now),
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
run_records = Table(
    "run_records", metadata,
    Column("id", String(32), primary_key=True),
    Column("user_id", String(16), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False),
    Column("chat_id", String(16), ForeignKey("chats.id", ondelete="CASCADE"), index=True, nullable=False),
    Column("question", Text, nullable=False),
    Column("status", String(20), nullable=False, default="running"),
    Column("events", JSON, default=list), Column("summary", JSON, default=dict),
    Column("started_at", DateTime, default=_now),
    Column("updated_at", DateTime, default=_now),
)
feedback = Table(
    "feedback", metadata,
    Column("id", String(16), primary_key=True, default=_id),
    Column("user_id", String(16), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False),
    Column("chat_id", String(16), ForeignKey("chats.id", ondelete="CASCADE"), index=True, nullable=False),
    Column("run_id", String(32), nullable=False, index=True),
    Column("rating", String(10), nullable=False),
    Column("category", String(40), nullable=False),
    Column("comment", Text, default=""),
    Column("created_at", DateTime, default=_now),
)
usage = Table(
    "usage", metadata,
    Column("user_id", String(64), primary_key=True), Column("day", String(10), primary_key=True),
    Column("questions", Integer, default=0), Column("usd", Float, default=0.0),
)


ai_profiles = Table(
    "ai_profiles", metadata,
    Column("user_id", String(16), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("mode", String(16), default="shared"), Column("provider", String(20), default="openai"),
    Column("model", String(200)), Column("credential_id", String(32)), Column("daily_limit", Float),
)
ai_credentials = Table(
    "ai_credentials", metadata,
    Column("id", String(32), primary_key=True),
    Column("user_id", String(16), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False),
    Column("provider", String(20), nullable=False), Column("ciphertext", Text, nullable=False),
    Column("suffix", String(4)), Column("active", Boolean, default=True),
    Column("created_at", DateTime, default=_now),
)
api_calls = Table(
    "api_calls", metadata,
    Column("id", String(32), primary_key=True), Column("user_id", String(16), index=True, nullable=False),
    Column("run_id", String(32), index=True), Column("day", String(10), index=True, nullable=False),
    Column("provider", String(20)), Column("model", String(200)), Column("credential_id", String(32)),
    Column("billing", String(20)), Column("agent", String(40)), Column("status", String(20)),
    Column("reserved_usd", Float), Column("usd", Float), Column("input_tokens", Integer, default=0),
    Column("cached_tokens", Integer, default=0), Column("output_tokens", Integer, default=0),
    Column("created_at", DateTime, default=_now),
)


class Store:
    def _upsert(self, table):
        if self.engine.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as dialect_insert
        else:
            from sqlalchemy.dialects.sqlite import insert as dialect_insert
        return dialect_insert(table)

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
        if self.engine.dialect.name == "postgresql":
            from sqlalchemy import inspect, text
            column = next(c for c in inspect(self.engine).get_columns("usage") if c["name"] == "user_id")
            if column["type"].length is not None and column["type"].length < 64:
                with self.engine.begin() as connection:
                    connection.execute(text("ALTER TABLE usage ALTER COLUMN user_id TYPE VARCHAR(64)"))

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

    # ------------------------------------------------------------------ run tracking and opt-in feedback
    def create_run(self, run_id: str, user_id: str, chat_id: str, question: str,
                   snapshot: str | None = None, use_defaults: bool = False, ai: dict | None = None) -> None:
        self._exec(insert(run_records).values(id=run_id, user_id=user_id, chat_id=chat_id,
                                               question=question, status="running", events=[],
                                               summary={"snapshot": snapshot, "use_defaults": use_defaults,
                                                        **({"ai": ai} if ai else {})}))

    def save_run(self, run_id: str, status: str, events: list[dict], summary: dict | None = None) -> None:
        values = {"status": status, "events": to_json(events), "updated_at": _now()}
        if summary is not None:
            values["summary"] = to_json(summary)
        self._exec(update(run_records).where(run_records.c.id == run_id).values(**values))

    def save_pause(self, run_id: str, chat_id: str, events: list[dict], event: dict) -> None:
        """The assistant's proposal and paused run become visible together; replay cannot duplicate messages."""
        mid = hashlib.sha256(f"{run_id}:{event.get('plan_version')}:{event.get('message')}".encode()).hexdigest()[:16]
        with self.engine.begin() as c:
            c.execute(update(run_records).where(run_records.c.id == run_id)
                      .values(status="paused", events=to_json(events), updated_at=_now()))
            c.execute(self._upsert(messages).values(id=mid, chat_id=chat_id, role="assistant",
                      content=event.get("message", ""), run=to_json(event)).on_conflict_do_nothing())

    def save_reply(self, run_id: str, chat_id: str, events: list[dict], message: str) -> None:
        with self.engine.begin() as c:
            changed = c.execute(update(run_records).where(run_records.c.id == run_id,
                                run_records.c.chat_id == chat_id, run_records.c.status == "paused")
                                .values(status="running", events=to_json(events), updated_at=_now()))
            if not changed.rowcount:
                raise ValueError("This proposal has already been answered")
            c.execute(insert(messages).values(id=_id(), chat_id=chat_id, role="user", content=message))

    def complete_run(self, run_id: str, final: dict, events: list[dict], summary: dict,
                     headline: str, message: dict) -> bool:
        """Commit terminal state, answer and spend once, atomically, including concurrent retries."""
        with self.engine.begin() as c:
            original = c.execute(select(run_records.c.summary).where(run_records.c.id == run_id)).scalar()
            summary = {**(original or {}), **summary}
            row = c.execute(update(run_records).where(run_records.c.id == run_id,
                            run_records.c.status.in_(["running", "paused"]))
                .values(status=final.get("status", "failed"), events=to_json(events),
                        summary=to_json(summary), updated_at=_now())
                .returning(run_records.c.user_id, run_records.c.chat_id)).mappings().first()
            if row is None:
                return False
            c.execute(insert(messages).values(id=_id(), chat_id=row["chat_id"], role="assistant",
                                              content=headline, run=to_json(message)))
            if (final.get("status") == "done" and final.get("grounded") and final.get("confirmed_plan")
                    and not any(r["result"].get("truncated") for r in final.get("analyses", []))):
                prior = c.execute(select(investigations.c.context)
                                  .where(investigations.c.chat_id == row["chat_id"])).scalar() or {}
                if final.get("relationship") == "new":
                    prior = {}
                compact_plan = [{k: v for k, v in item.items() if k != "sql"}
                                for item in final["confirmed_plan"]]
                evidence = {"run_id": run_id, "question": final.get("question"),
                            "plan": compact_plan, "finding": headline,
                            "data_as_of": final.get("data_as_of"), "snapshot": summary.get("snapshot"),
                            "results": [{"purpose": r.get("purpose"), "columns": r["result"]["columns"],
                                         "data": r["result"]["data"][:6], "row_count": len(r["result"]["data"]),
                                         "excerpted": len(r["result"]["data"]) > 6,
                                         "findings": r.get("analysis", {}).get("findings", []),
                                         "truncated": r["result"].get("truncated")}
                                        for r in final.get("analyses", [])],
                            "next_questions": (final.get("analysis") or {}).get("follow_up_questions", [])}
                context = {"objective": final.get("objective") or prior.get("objective") or final.get("question"),
                           "origin_plan": prior.get("origin_plan") or compact_plan,
                           "current_plan": compact_plan,
                           "metric_definitions": final.get("metric_definitions", {}),
                           "evidence": [*(prior.get("evidence") or []), evidence][-8:]}
                c.execute(self._upsert(investigations).values(chat_id=row["chat_id"], context=to_json(context))
                          .on_conflict_do_update(index_elements=[investigations.c.chat_id],
                                                set_={"context": to_json(context), "updated_at": _now()}))
            c.execute(update(chats).where(chats.c.id == row["chat_id"]).values(updated_at=_now()))
            usd = 0.0 if summary.get("ai") else (final.get("cost") or {}).get("usd", 0.0)
            c.execute(self._upsert(usage).values(user_id=row["user_id"], day=str(datetime.now(UTC).date()),
                                               questions=0, usd=usd)
                .on_conflict_do_update(index_elements=[usage.c.user_id, usage.c.day], set_={"usd": usage.c.usd + usd}))
            return True

    def run_record(self, user_id: str, run_id: str) -> dict | None:
        return self._one(select(run_records).where(run_records.c.id == run_id,
                                                      run_records.c.user_id == user_id))

    def pending_run(self, user_id: str, chat_id: str) -> dict | None:
        return self._one(select(run_records).where(run_records.c.user_id == user_id,
                                                      run_records.c.chat_id == chat_id,
                                                      run_records.c.status.in_(["running", "paused"]))
                         .order_by(run_records.c.started_at.desc()).limit(1))

    def add_feedback(self, user_id: str, chat_id: str, run_id: str, rating: str,
                     category: str, comment: str) -> None:
        chat = self.chat(user_id, chat_id)
        if not chat:
            raise ValueError("Chat not found")
        valid = any((msg.get("run") or {}).get("run_id") == run_id
                    for msg in self.messages(chat_id) if msg["role"] == "assistant")
        if not valid:
            raise ValueError("Completed answer not found in this chat")
        existing = self._one(select(feedback).where(feedback.c.user_id == user_id,
                                                     feedback.c.run_id == run_id))
        if existing:
            self._exec(update(feedback).where(feedback.c.id == existing["id"])
                       .values(rating=rating, category=category, comment=comment))
        else:
            self._exec(insert(feedback).values(id=_id(), user_id=user_id, chat_id=chat_id,
                                                      run_id=run_id, rating=rating,
                                                      category=category, comment=comment))

    def feedback_export(self) -> list[dict]:
        """Only consented feedback; private user IDs and full questions are intentionally excluded."""
        rows = self._all(select(feedback).order_by(feedback.c.created_at.desc()).limit(1000))
        return [{"run_id": r["run_id"], "rating": r["rating"], "category": r["category"],
                 "comment": r["comment"], "created_at": r["created_at"].isoformat()} for r in rows]

    def run_metrics(self) -> dict:
        rows = self._all(select(run_records.c.status, run_records.c.summary)
                         .where(run_records.c.status.not_in(["running", "paused"]))
                         .order_by(run_records.c.started_at.desc()).limit(200))
        stages: dict[str, list[float]] = {}
        for row in rows:
            for step, seconds in (row["summary"] or {}).get("stages", {}).items():
                stages.setdefault(step, []).append(float(seconds))
        return {"completed_runs": len(rows),
                "status_counts": {s: sum(r["status"] == s for r in rows)
                                  for s in sorted({r["status"] for r in rows})},
                "mean_stage_seconds": {k: round(sum(v) / len(v), 2) for k, v in stages.items()}}

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

    # ------------------------------------------------------------------ provider selection and credentials
    def ai_profile(self, user_id: str) -> dict:
        row = self._one(select(ai_profiles).where(ai_profiles.c.user_id == user_id)) or {}
        return {"mode": "shared", "provider": "openai", "model": get_settings().model_main.split(":", 1)[-1],
                "credential_id": None, "daily_limit": None, **{k: v for k, v in row.items() if v is not None}}

    def set_ai_profile(self, user_id: str, **values) -> None:
        fields = ('mode', 'provider', 'model', 'credential_id', 'daily_limit')
        allowed = {k: v for k, v in values.items() if k in fields}
        self._exec(self._upsert(ai_profiles).values(user_id=user_id, **allowed).on_conflict_do_update(
            index_elements=[ai_profiles.c.user_id], set_=allowed))

    def add_credential(self, user_id: str, provider: str, key: str, model: str) -> str:
        from .providers import encrypt_key
        cid = uuid.uuid4().hex
        encrypted = encrypt_key(user_id, cid, provider, key)
        with self.engine.begin() as c:
            # Replacement revokes the old version. Pending runs fail explicitly, never use the replacement key.
            c.execute(update(ai_credentials).where(ai_credentials.c.user_id == user_id)
                      .values(active=False, ciphertext=""))
            c.execute(insert(ai_credentials).values(id=cid, user_id=user_id, provider=provider,
                      ciphertext=encrypted, suffix=key[-4:], active=True))
            values = dict(mode="personal", provider=provider, model=model, credential_id=cid)
            c.execute(self._upsert(ai_profiles).values(user_id=user_id, **values).on_conflict_do_update(
                index_elements=[ai_profiles.c.user_id], set_=values))
        return cid

    def credential(self, user_id: str, cid: str | None) -> dict | None:
        return self._one(select(ai_credentials).where(ai_credentials.c.id == cid,
                          ai_credentials.c.user_id == user_id, ai_credentials.c.active.is_(True))) if cid else None

    def remove_credentials(self, user_id: str) -> None:
        with self.engine.begin() as c:
            c.execute(update(ai_credentials).where(ai_credentials.c.user_id == user_id)
                      .values(active=False, ciphertext="", suffix=""))
            c.execute(update(ai_profiles).where(ai_profiles.c.user_id == user_id).values(
                mode="shared", provider="openai", model=get_settings().model_main.split(":", 1)[-1],
                credential_id=None))

    def pin_ai(self, user: dict) -> dict:
        import os

        from .providers import key_version
        profile = self.ai_profile(user['id'])
        return {"user_id": user['id'], "mode": profile['mode'], "provider": profile['provider'],
                "model": profile['model'],
                "credential_id": profile.get('credential_id') if profile['mode'] == 'personal' else None,
                "shared_version": key_version(os.getenv('OPENAI_API_KEY', '')) if profile['mode'] == 'shared' else None,
                "billing": "personal" if profile['mode'] == 'personal' else "admin" if user['is_admin'] else "shared"}

    def ai_key(self, pin: dict) -> str:
        from .providers import ProviderError, decrypt_key, key_version, shared_key
        if pin['mode'] == 'personal':
            credential = self.credential(pin['user_id'], pin['credential_id'])
            if not credential or credential['provider'] != pin['provider']:
                raise ProviderError(
                    'This run’s credential was revoked. Start a new request with your current settings.', 409)
            return decrypt_key(pin['user_id'], credential)
        key = shared_key()
        if key_version(key) != pin['shared_version']:
            raise ProviderError(
                'Pantree credentials changed. Start a new request; this run will not switch credentials.', 409)
        return key

    def reserve_api_call(self, pin: dict, run_id: str, agent: str, amount: float) -> str:
        from .llm import BUDGET_KEY, BudgetExceeded
        cid, today = uuid.uuid4().hex, str(datetime.now(UTC).date())
        if amount < 0 or not math.isfinite(amount):
            raise ValueError('Invalid reservation')
        with self.engine.begin() as c:
            if pin['billing'] == 'shared':
                profile = self.ai_profile(pin['user_id'])
                limit = profile['daily_limit'] if profile.get('daily_limit') is not None else \
                    get_settings().daily_user_budget_usd
                for identity, budget in [(BUDGET_KEY, get_settings().daily_budget_usd), (pin['user_id'], limit)]:
                    c.execute(self._upsert(usage).values(user_id=identity, day=today, usd=0, questions=0)
                              .on_conflict_do_nothing())
                    changed = c.execute(update(usage).where(usage.c.user_id == identity, usage.c.day == today,
                        usage.c.usd + amount <= budget).values(usd=usage.c.usd + amount)).rowcount
                    if not changed:
                        raise BudgetExceeded('The shared daily spending allowance cannot cover this call. '
                                             'Connect a personal API key or try again tomorrow.')
            c.execute(insert(api_calls).values(id=cid, user_id=pin['user_id'], run_id=run_id, day=today,
                provider=pin['provider'], model=pin['model'], credential_id=pin.get('credential_id'),
                billing=pin['billing'], agent=agent, status='reserved', reserved_usd=amount))
        return cid

    def settle_api_call(self, cid: str, usd: float | None, tokens: dict, status: str = 'succeeded') -> None:
        from .llm import BUDGET_KEY
        with self.engine.begin() as c:
            row = c.execute(update(api_calls).where(api_calls.c.id == cid, api_calls.c.status == 'reserved')
                .values(status=status, usd=usd, input_tokens=tokens.get('input', 0),
                        cached_tokens=tokens.get('cached', 0), output_tokens=tokens.get('output', 0))
                .returning(api_calls)).mappings().first()
            if not row:
                return
            if row['billing'] == 'shared':
                # Unknown outcomes keep their reservation charged. Never free a possibly billed call on a timeout.
                charged = row['reserved_usd'] if usd is None else usd
                for identity in (BUDGET_KEY, row['user_id']):
                    c.execute(update(usage).where(usage.c.user_id == identity, usage.c.day == row['day'])
                              .values(usd=usage.c.usd + charged - row['reserved_usd']))

    def api_usage(self, user_id: str | None = None) -> dict:
        today = str(datetime.now(UTC).date())
        stmt = select(api_calls).where(api_calls.c.day == today)
        if user_id is not None:
            stmt = stmt.where(api_calls.c.user_id == user_id)
        rows = self._all(stmt)
        groups = {}
        for row in rows:
            key = (row['user_id'], row['billing'], row['provider'], row['model'])
            group = groups.setdefault(key, dict(user_id=key[0], billing=key[1], provider=key[2], model=key[3],
                calls=0, input_tokens=0, cached_tokens=0, output_tokens=0, usd=0, uncertain_calls=0, reserved_usd=0))
            group['calls'] += 1
            for field in ('input_tokens', 'cached_tokens', 'output_tokens'):
                group[field] += row[field] or 0
            group['usd'] += row['usd'] or 0
            if row['usd'] is None:
                group['uncertain_calls'] += 1
                group['reserved_usd'] += row['reserved_usd'] or 0
        result = {"day": today, "timezone": "UTC", "groups": list(groups.values())}
        if user_id is not None:
            profile = self.ai_profile(user_id)
            limit = (profile['daily_limit'] if profile.get('daily_limit') is not None
                     else get_settings().daily_user_budget_usd)
            used = self.usd_today(user_id)
            result.update(requests=self.questions_today(user_id), shared_used=used,
                          daily_limit=limit, remaining=max(0, limit-used))
        return result

    def api_call_history(self, run_id: str, user_id: str) -> list[dict]:
        return self._all(select(api_calls).where(api_calls.c.run_id == run_id, api_calls.c.user_id == user_id)
                         .order_by(api_calls.c.created_at))

    # ------------------------------------------------------------------ email codes
    def new_email_code(self, email: str, minutes: int = 10) -> str | None:
        """A 6-digit code, or None if one was sent less than 60 seconds ago (rate limit)."""
        email = email.strip().lower()
        code = f"{secrets.randbelow(10**6):06d}"
        values = {"code_hash": hashlib.sha256(f"{email}:{code}".encode()).hexdigest(),
                  "expires_at": _now() + timedelta(minutes=minutes), "attempts": 0, "sent_at": _now()}
        with self.engine.begin() as c:
            inserted = c.execute(self._upsert(email_codes).values(email=email, **values).on_conflict_do_update(
                index_elements=[email_codes.c.email], set_=values,
                where=email_codes.c.sent_at <= _now() - timedelta(seconds=60)).returning(email_codes.c.email)).first()
        return code if inserted else None

    def check_email_code(self, email: str, code: str) -> bool:
        email = email.strip().lower()
        with self.engine.begin() as c:
            row = c.execute(update(email_codes).where(email_codes.c.email == email,
                email_codes.c.expires_at >= _now(), email_codes.c.attempts < 5)
                .values(attempts=email_codes.c.attempts + 1).returning(email_codes.c.code_hash)).first()
            digest = hashlib.sha256(f"{email}:{code.strip()}".encode()).hexdigest()
            if not row or not secrets.compare_digest(row[0], digest):
                return False
            return c.execute(delete(email_codes).where(email_codes.c.email == email,
                                                       email_codes.c.code_hash == row[0])).rowcount == 1

    # ------------------------------------------------------------------ usage
    def questions_today(self, user_id: str) -> int:
        row = self._one(select(usage).where(usage.c.user_id == user_id, usage.c.day == str(datetime.now(UTC).date())))
        return row["questions"] if row else 0

    def count_question(self, user_id: str, usd: float = 0.0) -> None:
        self.reserve_question(user_id, usd=usd)

    def reserve_question(self, user_id: str, limit: int | None = None, usd: float = 0.0) -> bool:
        """Check the daily quota and increment it atomically, including concurrent first requests."""
        if limit is not None and limit < 1:
            return False
        statement = self._upsert(usage).values(user_id=user_id, day=str(datetime.now(UTC).date()), questions=1, usd=usd)
        statement = statement.on_conflict_do_update(index_elements=[usage.c.user_id, usage.c.day],
            set_={"questions": usage.c.questions + 1, "usd": usage.c.usd + usd},
            where=usage.c.questions < limit if limit is not None else None)
        with self.engine.begin() as c:
            return c.execute(statement.returning(usage.c.user_id)).first() is not None

    def usd_today(self, key: str) -> float:
        row = self._one(select(usage).where(usage.c.user_id == key, usage.c.day == str(datetime.now(UTC).date())))
        return float(row["usd"]) if row else 0.0

    def add_usd_today(self, key: str, usd: float) -> None:
        """Add spend for today under a key (atomic update; the row is created on first use)."""
        with self.engine.begin() as c:
            c.execute(self._upsert(usage).values(user_id=key, day=str(datetime.now(UTC).date()),
                                               questions=0, usd=usd)
                .on_conflict_do_update(index_elements=[usage.c.user_id, usage.c.day], set_={"usd": usage.c.usd + usd}))

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
                c.execute(delete(investigations).where(investigations.c.chat_id == chat_id))
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
        return self.conversation(chat_id, turns)[0]

    def investigation(self, user_id: str, chat_id: str) -> dict:
        row = self._one(select(investigations.c.context).join(chats, chats.c.id == investigations.c.chat_id)
                        .where(chats.c.user_id == user_id, chats.c.id == chat_id))
        return row["context"] if row else {}

    def conversation(self, chat_id: str, turns: int = 4) -> tuple[str, dict | None]:
        """Fetch bounded history and the latest completed plan in one database roundtrip."""
        rows = list(reversed(self._all(select(messages).where(messages.c.chat_id == chat_id)
                    .order_by(messages.c.created_at.desc()).limit(max(1, min(turns, 20)) * 2))))
        previous = None
        lines = []
        for m in rows:
            if m["role"] == "user":
                lines.append(f"User: {m['content']}")
            else:
                run = m.get("run") or {}
                if run.get("status") == "done":
                    previous = run.get("spec")
                plan = run.get("restated") or run.get("readback")
                lines.append(f"Analyst: {m['content']}" + (f" (analysed: {plan})" if plan else ""))
        return "\n".join(lines), previous

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


def to_json(value: Any) -> Any:
    """Make DB rows JSON-friendly (datetimes as ISO strings)."""
    return json.loads(json.dumps(value, default=lambda v: v.isoformat() if hasattr(v, "isoformat") else str(v)))
