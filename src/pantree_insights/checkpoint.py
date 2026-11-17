"""LangGraph checkpoint backend.

Development/tests use memory. Production can persist checkpoints in Postgres so a
clarification can resume after a container restart. The caller owns this object for
the process lifetime and closes it on shutdown.
"""
from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass

from langgraph.checkpoint.memory import InMemorySaver

from .config import get_settings


@dataclass
class CheckpointBackend:
    saver: object
    context: AbstractContextManager | None = None

    def close(self) -> None:
        if self.context:
            self.context.__exit__(None, None, None)
            self.context = None


def build_checkpointer() -> CheckpointBackend:
    settings = get_settings()
    url = settings.checkpoint_database_url.strip()
    if not url:
        return CheckpointBackend(InMemorySaver())

    from langgraph.checkpoint.postgres import PostgresSaver

    if url.startswith("postgresql+psycopg://"):
        url = "postgresql://" + url[len("postgresql+psycopg://"):]
    ctx = PostgresSaver.from_conn_string(url)
    saver = ctx.__enter__()
    saver.setup()
    return CheckpointBackend(saver=saver, context=ctx)
