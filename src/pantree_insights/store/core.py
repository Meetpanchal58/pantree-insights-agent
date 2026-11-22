"""Store: core."""
from __future__ import annotations

from functools import lru_cache

from sqlalchemy import (
    create_engine,
)
from sqlalchemy.engine import Engine

from ..config import get_settings
from .accounts import AccountStore
from .chats import ChatStore
from .memory import MemoryStore
from .models import metadata
from .quotas import QuotaStore
from .runs import RunStore


class Store(RunStore, AccountStore, QuotaStore, ChatStore, MemoryStore):
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


@lru_cache
def get_store() -> Store:
    return Store()
