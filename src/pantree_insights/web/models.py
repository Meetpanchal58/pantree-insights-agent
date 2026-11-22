"""Web: models."""
from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field, SecretStr

from .. import providers

log = logging.getLogger("pantree.web")

class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    instructions: str = Field("", max_length=4000)


class ProjectPatch(BaseModel):
    name: str | None = Field(None, max_length=200)
    instructions: str | None = Field(None, max_length=4000)


class ChatIn(BaseModel):
    project_id: str | None = None


class ChatPatch(BaseModel):
    title: str | None = Field(None, max_length=200)
    project_id: str | None = None
    move: bool = False                     # True: set project_id (None = out of any project)


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    plan_version: int | None = Field(None, ge=0)
    pending_run_id: str | None = Field(None, max_length=32)
    idempotency_key: str | None = Field(None, min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class EditMessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=2000)
    expected_content: str = Field(max_length=2000)
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    pending_run_id: str | None = None
    plan_version: int | None = Field(None, ge=0)


class AnswerIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    plan_version: int = Field(ge=0)
    idempotency_key: str | None = Field(None, min_length=8, max_length=128)


class FeedbackIn(BaseModel):
    chat_id: str = Field(min_length=1, max_length=32)
    run_id: str = Field(min_length=1, max_length=32)
    rating: str = Field(pattern="^(positive|negative)$")
    category: str = Field("other", max_length=40)
    comment: str = Field("", max_length=1000)


class PreferencesIn(BaseModel):
    text: str = Field("", max_length=4000)


class AISettingsIn(BaseModel):
    mode: Literal['shared', 'personal']
    provider: providers.Provider = 'openai'
    model: str = Field(min_length=1, max_length=200)
    api_key: SecretStr | None = Field(None, min_length=8, max_length=512)


class ConnectionIn(BaseModel):
    provider: providers.Provider
    api_key: SecretStr = Field(min_length=8, max_length=512)


class AllowanceIn(BaseModel):
    daily_limit: float = Field(ge=0, le=10000, allow_inf_nan=False)
