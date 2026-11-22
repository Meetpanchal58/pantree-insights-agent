"""Store: models."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

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
)

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
