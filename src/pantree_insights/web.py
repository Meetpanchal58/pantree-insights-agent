"""The web app (FastAPI): chats and projects, live agent runs over Server-Sent Events, memory, pulse, demos.

    pantree-web                      # http://127.0.0.1:8000

A question runs in a background thread; its progress events are kept in memory and streamed to the
browser (GET /api/runs/{id}/stream). Clarifying questions pause the run; POST /api/runs/{id}/answer
resumes it. Finished runs are saved as chat messages (with their events, so they can be replayed).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr
from starlette.middleware.sessions import SessionMiddleware

from . import demos as demo_runs
from . import explorer, providers
from .auth import current_user, methods, require_admin, require_user
from .auth import router as auth_router
from .config import get_settings
from .db import manifest, snapshot_signature
from .llm import BudgetExceeded, ChatLLM
from .store import MemoryScope, Store, get_store, to_json

log = logging.getLogger("pantree.web")
STATIC = Path(__file__).parent / "static"


# ----------------------------------------------------------------------------- request bodies
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


# ----------------------------------------------------------------------------- background runs
@dataclass
class Run:
    id: str
    user: dict
    chat: dict
    question: str
    started: float = field(default_factory=time.time)
    events: list[dict] = field(default_factory=list)
    message_id: str | None = None
    last_message_id: str | None = None
    paused: bool = False              # waiting for clarifying answers
    finished: bool = False
    segment_done: threading.Event = field(default_factory=threading.Event)
    agent: object | None = None
    ai: dict | None = None


def failure_details(error: Exception) -> dict:
    """User-facing failures without raw provider/credential details."""
    if isinstance(error, BudgetExceeded):
        return {"message": str(error), "error_code": "budget_exhausted", "retryable": False}
    if isinstance(error, providers.ProviderError):
        return {"message": str(error), "error_code": "provider_error", "retryable": error.status_code >= 429}
    status = getattr(error, "status_code", None)
    name = type(error).__name__.lower()
    headers = getattr(getattr(error, "response", None), "headers", {}) or {}
    try:
        wait = min(max(int(headers.get("retry-after", 30)), 1), 300)
    except (TypeError, ValueError):
        wait = 30
    if status == 429 or "ratelimit" in name:
        return {"message": f"The model service is rate-limited. Your chat is saved. Try again in {wait} seconds.",
                "error_code": "rate_limit", "retry_after": wait, "retryable": True}
    if isinstance(error, TimeoutError) or "timeout" in name:
        return {"message": "The model service took too long. Your chat is saved; you can try the question again.",
                "error_code": "timeout", "retryable": True}
    return {"message": "The analysis could not finish. Your chat is saved; please try again.",
            "error_code": "analysis_failed", "retryable": True}


class Runner:
    """Runs execute in one process; persisted paused checkpoints can be reattached after a restart."""

    def __init__(self, agent_factory=None):
        self._agent_factory = agent_factory
        self._agent = None
        self._snapshot = None
        self._lock = threading.Lock()
        self.admission_lock = threading.Lock()
        self.runs: dict[str, Run] = {}
        self._slots = threading.BoundedSemaphore(max(1, get_settings().max_concurrent_runs))
        self._waiting = 0                                   # runs queued for a free slot (shown to the user)

    @property
    def agent(self):
        with self._lock:
            signature = snapshot_signature() if self._agent_factory is None else None
            if self._agent is None or signature != self._snapshot:
                from .graph import Agent
                old = self._agent
                self._agent = (self._agent_factory or Agent)()
                self._snapshot = signature
                if (old is not None and hasattr(old, "close")
                        and not any(not r.finished and r.agent is old for r in self.runs.values())):
                    old.close()
            return self._agent

    def start(self, store: Store, user: dict, chat: dict, question: str, use_defaults: bool = False,
              run_id: str | None = None, initial_answers: dict | None = None,
              recorded_message_id: str | None = None) -> Run:
        """Return at once: the agent (built on the first question after a restart, which can take a while on a
        cold instance) is prepared in the run's own thread, shown as a step, never inside the HTTP request.
        A failure while preparing ends the run with a saved failure, so the question always gets a reply."""
        run_id = run_id or uuid.uuid4().hex[:12]
        history, previous = store.conversation(chat["id"])
        investigation = {**store.investigation(user["id"], chat["id"]),
                         **store.recall(user["id"], chat["id"], question, snapshot_signature())}
        if not previous and investigation.get("current_plan"):
            previous = investigation["current_plan"][0].get("spec")
        message_id = recorded_message_id or store.add_message(chat["id"], "user", question,
            run={"kind": "question", "run_id": run_id})
        if chat["title"] == "New chat":
            store.update_chat(user["id"], chat["id"], title=question[:80])
        scope = MemoryScope(store, user, chat)
        run = Run(run_id, user, chat, question, ai=store.pin_ai(user), message_id=message_id)
        self.runs[run.id] = run
        store.create_run(run.id, user["id"], chat["id"], question, snapshot=snapshot_signature(),
                         use_defaults=use_defaults, ai=run.ai, message_id=message_id)

        def events():
            cold = self._agent is None
            started = time.time()
            if cold:
                yield {"type": "step", "step": "prepare", "status": "started", "t": started}
            try:
                agent = self.agent
            except Exception:
                if cold:
                    yield {"type": "step", "step": "prepare", "status": "failed", "t": time.time(),
                           "detail": {"seconds": round(time.time() - started, 2)}}
                raise
            run.agent = agent
            if self._agent_factory is None:
                agent._llms[run.id] = ChatLLM(selection=run.ai, store=store, run_id=run.id)
            if cold:
                yield {"type": "step", "step": "prepare", "status": "done", "t": time.time(),
                       "detail": {"seconds": round(time.time() - started, 2)}}
            _, stream = agent.start(question, chat_id=chat["id"], history=history, use_defaults=use_defaults,
                                    memory=scope, run_id=run.id, investigation=investigation,
                                    initial_answers=initial_answers,
                                    **({"previous_spec": previous} if previous else {}))
            yield from stream
        self._consume(store, run, events())
        return run

    def restore(self, store: Store, user: dict, run_id: str) -> Run | None:
        """Restore a paused user-owned checkpoint; never restart a pending LLM call."""
        run = self.runs.get(run_id)
        if run:
            return run
        saved = store.run_record(user["id"], run_id)
        if not saved or saved["status"] != "paused":
            return None
        if get_settings().checkpoint_backend != "postgres":
            # An InMemorySaver checkpoint cannot survive a process restart.
            store.save_run(run_id, "interrupted", saved.get("events") or [])
            return None
        old_snapshot = (saved.get("summary") or {}).get("snapshot")
        if old_snapshot and old_snapshot != snapshot_signature():
            store.save_run(run_id, "interrupted", saved.get("events") or [],
                           {"reason": "The dataset changed during a paused analysis"})
            return None
        chat = store.chat(user["id"], saved["chat_id"])
        if chat is None:
            return None
        agent = self.agent
        run = Run(run_id, user, chat, saved["question"], agent=agent, ai=(saved.get('summary') or {}).get('ai'))
        if self._agent_factory is None:
            if not run.ai:
                store.save_run(run_id, 'interrupted', saved.get('events') or [])
                return None  # pre-migration pauses have no pinned credential identity
            agent._llms[run_id] = ChatLLM(selection=run.ai, store=store, run_id=run_id)
        run.events = saved["events"] or []
        run.started = min((e.get("t", time.time()) for e in run.events), default=time.time())
        run.paused = True
        agent.restore_memory(run_id, MemoryScope(store, user, chat))
        self.runs[run_id] = run
        return run

    def answer(self, store: Store, run: Run, message: str, plan_version: int,
               response_id: str | None = None, edit: dict | None = None) -> int:
        # admission_lock serializes retries and different replies within this single-instance application.
        if response_id:
            for i, event in enumerate(run.events):
                if event.get("response_id") == response_id:
                    if (event.get("message") != message or event.get("plan_version") != plan_version
                            or event.get("edited_message_id") != (edit or {}).get("id")):
                        raise HTTPException(409, "This reply key was already used for a different message.")
                    return i
        latest = next((e for e in reversed(run.events) if e["type"] == "clarify"), {})
        if not run.paused or plan_version != latest.get("plan_version"):
            raise HTTPException(409, "The pending plan changed. Reopen the chat and reply to the latest plan.")
        since = len(run.events)
        event = {"type": "answered", "message": message, "plan_version": plan_version,
                 "response_id": response_id, "edited_message_id": (edit or {}).get("id"), "t": time.time()}
        if store is not None:
            try:
                run.last_message_id = store.save_reply(run.id, run.chat["id"], [*run.events, event], message, edit)
            except ValueError as error:
                raise HTTPException(409, str(error)) from error
        run.paused = False
        run.segment_done.clear()
        run.events.append(event)
        self._consume(store, run, (run.agent or self.agent).resume(run.id, message, plan_version))
        return since

    def _consume(self, store: Store, run: Run, events) -> None:
        def work():
            queued = not self._slots.acquire(blocking=False)
            if queued:                                      # a bounded pool: a burst of users queues, visibly
                with self._lock:
                    self._waiting += 1
                    position = self._waiting
                started = time.time()
                run.events.append({"type": "step", "step": "queue", "status": "started", "t": started,
                                   "detail": {"position": position}})
                self._slots.acquire()
                with self._lock:
                    self._waiting -= 1
                run.events.append({"type": "step", "step": "queue", "status": "done", "t": time.time(),
                                   "detail": {"seconds": round(time.time() - started, 2)}})
            try:
                for event in events:
                    event = {**event, "t": event.get("t", time.time())}
                    if run.ai:
                        event['ai'] = {k: run.ai[k] for k in ('mode', 'provider', 'model')}
                    if event["type"] == "clarify":
                        store.save_pause(run.id, run.chat["id"], [*run.events, event], event)
                        run.paused = True
                    elif event["type"] == "final":
                        self._save(store, run, event)
                    # A terminal event must not reach the browser before its chat/feedback record exists.
                    run.events.append(event)
                    if event["type"] == "final":
                        run.finished = True
            except Exception as error:                      # never leave the browser waiting
                log.exception("run %s failed", run.id)
                final = {"type": "final", "run_id": run.id, "question": run.question,
                         "status": "failed", "t": time.time(),
                         **failure_details(error)}
                self._save(store, run, final)
                run.events.append(final)
                run.finished = True
            finally:
                self._slots.release()
                run.segment_done.set()
        threading.Thread(target=work, daemon=True, name=f"run-{run.id}").start()

    def _save(self, store: Store, run: Run, final: dict) -> None:
        headline = ((final.get("analysis") or {}).get("headline") or final.get("message") or "")
        timeline = [{**e, "t": round(e["t"] - run.started, 2)} for e in run.events if e["type"] != "final"]
        stage_times = {}
        for event in run.events:
            if event.get("type") == "step" and event.get("status") != "started":
                detail = event.get("detail") or {}
                if isinstance(detail.get("seconds"), (float, int)):
                    stage_times[event["step"]] = round(stage_times.get(event["step"], 0) + detail["seconds"], 2)
        calls = final.get("calls") or []
        store.complete_run(run.id, final, [*run.events, final],
                       {"stages": stage_times, "cost_usd": (final.get("cost") or {}).get("usd", 0),
                        "llm_calls": len(calls), "trust": final.get("trust"),
                        "verification": (final.get("verification") or {}).get("status")},
                       headline, {**final, "events": timeline})
        run.agent = None  # Finished runs no longer need to retain an older snapshot's entire agent/index.

    def prune(self) -> None:
        """Completed histories are durable; retain only a bounded recent set for live stream reconnects."""
        completed = sorted((r for r in self.runs.values() if r.finished and r.segment_done.is_set()),
                           key=lambda r: r.started, reverse=True)
        for run in completed[128:]:
            self.runs.pop(run.id, None)


runner = Runner()


# ----------------------------------------------------------------------------- app
@asynccontextmanager
async def lifespan(app: FastAPI):
    def warm():
        try:
            _ = runner.agent                                  # the agent first: questions must not wait for pulse
        except Exception:
            log.exception("agent failed to start")
    threading.Thread(target=warm, daemon=True).start()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    if settings.app_env == "production" and (len(settings.session_secret.strip()) < 32
                                              or settings.session_secret.startswith("dev-only")):
        raise RuntimeError("Set a random SESSION_SECRET of at least 32 characters in production.")
    app = FastAPI(title="Pantree Insights", lifespan=lifespan, docs_url=None, redoc_url=None)
    @app.middleware("http")
    async def request_reference(request: Request, call_next):
        reference = uuid.uuid4().hex[:16]
        response = await call_next(request)
        response.headers["X-Request-ID"] = reference
        if response.status_code in (429, 503, 504):
            log.warning("request_busy method=%s path=%s status=%s request_id=%s",
                        request.method, request.url.path, response.status_code, reference)
        return response
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, same_site="lax",
                       https_only=settings.app_env == "production", max_age=30 * 24 * 3600)
    @app.exception_handler(providers.ProviderError)
    async def provider_error(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=exc.status_code,
                            headers={'Cache-Control': 'no-store'})

    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({'detail': [{'loc': e['loc'], 'type': e['type'], 'msg': e['msg']}
                                       for e in exc.errors()]}, status_code=422)

    app.include_router(auth_router)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.middleware("http")
    async def json_only_for_writes(request: Request, call_next):
        """Basic CSRF protection: state-changing API calls must be JSON (cross-site forms cannot send it)."""
        writes = request.method in ("POST", "PATCH", "PUT", "DELETE")
        protected = request.url.path.startswith(("/api/", "/auth/email", "/auth/logout"))
        if writes and protected and "application/json" not in request.headers.get("content-type", ""):
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "JSON required"}, status_code=415)
        return await call_next(request)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/healthz")
    def health():
        return {"ok": True}

    @app.get("/api/ready")
    async def ready(user=Depends(require_user)):
        # Holding an authenticated request open gives request-based Cloud Run CPU to initialization.
        # The browser stays usable; this performs no inference and reserves no analytical allowance.
        started = time.perf_counter()
        try:
            await run_in_threadpool(lambda: runner.agent)
        except Exception:
            log.exception("analyst_readiness failed")
            return JSONResponse({"ready": False, "message": "Data preparation failed. You can retry opening the app."},
                                status_code=503, headers={"Cache-Control": "no-store"})
        return JSONResponse({"ready": True, "seconds": round(time.perf_counter()-started, 3)},
                            headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------------ me
    @app.get("/api/me")
    def me(user: dict | None = Depends(current_user), store: Store = Depends(get_store)):
        info = manifest()
        base = {"signin": methods(), "data": {"size": info["size"], "first_day": info["first_day"],
                                              "last_day": info["last_day"]}}
        if not user:
            return {**base, "user": None}
        used = store.questions_today(user["id"])
        return {**base, "user": {k: user[k] for k in ("id", "email", "name", "picture", "is_admin")},
                "limit": {"per_day": settings.daily_question_limit, "used": used,
                          "unlimited": bool(user["is_admin"])}, "ai": public_ai(store, user)}

    # ------------------------------------------------------------------ dataset browsing (no LLM or quota)
    @app.get('/api/dataset')
    def dataset(user=Depends(require_user)):
        return JSONResponse(explorer.overview(), headers={'Cache-Control': 'private, no-store'})

    @app.get('/api/dataset/{table}')
    def dataset_table(table: str, version: str | None = None, user=Depends(require_user)):
        try:
            result = explorer.preview(table, version)
        except KeyError:
            raise HTTPException(404, 'Table not available') from None
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        return JSONResponse(result, headers={'Cache-Control': 'private, no-store'})

    # ------------------------------------------------------------------ AI settings and accounting
    def public_ai(store, user):
        profile = store.ai_profile(user['id'])
        credential = store.credential(user['id'], profile.get('credential_id'))
        import os
        return {**profile, 'credential_id': None, 'user_id': None,
            'default_provider': 'openai', 'default_model': settings.model_main.split(':', 1)[-1],
            'light_model': settings.model_light, 'encryption_ready': providers.encryption_ready(),
            'shared_configured': bool(os.getenv('OPENAI_API_KEY')),
            'personal': {'provider': credential['provider'], 'suffix': credential['suffix']} if credential else None,
            'usage': store.api_usage(user['id']), 'exempt': bool(user['is_admin']) or profile['mode'] == 'personal'}

    @app.get('/api/ai/settings')
    def ai_settings(user=Depends(require_user), store: Store = Depends(get_store)):
        return JSONResponse(public_ai(store, user), headers={'Cache-Control': 'no-store'})

    def account_key(store, user, mode, provider):
        if mode == 'shared':
            if provider != 'openai':
                raise HTTPException(400, 'Pantree default credentials use OpenAI')
            return providers.shared_key()
        profile = store.ai_profile(user['id'])
        credential = store.credential(user['id'], profile.get('credential_id'))
        if not credential or credential['provider'] != provider:
            raise HTTPException(409, 'Connect a key for this provider first')
        return providers.decrypt_key(user['id'], credential)

    @app.get('/api/ai/models')
    def ai_models(mode: Literal['shared', 'personal'] = 'shared', provider: providers.Provider = 'openai',
                  user=Depends(require_user), store: Store = Depends(get_store)):
        key = account_key(store, user, mode, provider)
        return JSONResponse({'models': providers.discover(provider, key, user['id'])},
                            headers={'Cache-Control': 'no-store'})

    @app.post('/api/ai/test')
    def ai_test(body: ConnectionIn, user=Depends(require_user)):
        if not providers.encryption_ready():
            raise HTTPException(503, 'The server must configure credential encryption before connecting personal keys')
        models = providers.discover(body.provider, body.api_key.get_secret_value(), user['id'], refresh=True)
        return {'connected': True, 'models': models,
                'message': 'Credentials accepted by the model listing API. No billable inference was performed.'}

    @app.put('/api/ai/settings')
    def save_ai_settings(body: AISettingsIn, user=Depends(require_user), store: Store = Depends(get_store)):
        if body.mode == 'shared' and (body.provider != 'openai' or body.api_key):
            raise HTTPException(400, 'Personal keys cannot be saved as Pantree default credentials')
        key = body.api_key.get_secret_value() if body.api_key else account_key(store, user, body.mode, body.provider)
        models = providers.discover(body.provider, key, user['id'])
        if body.model not in {m['id'] for m in models}:
            raise HTTPException(400, 'Select a compatible model returned by your active provider account')
        if body.api_key:
            store.add_credential(user['id'], body.provider, key, body.model)
        else:
            store.set_ai_profile(user['id'], mode=body.mode, provider=body.provider, model=body.model)
        return public_ai(store, user)

    @app.delete('/api/ai/credentials')
    def remove_ai_credentials(user=Depends(require_user), store: Store = Depends(get_store)):
        store.remove_credentials(user['id'])
        return public_ai(store, user)

    @app.get('/api/admin/usage')
    def admin_usage(user=Depends(require_admin), store: Store = Depends(get_store)):
        from sqlalchemy import select

        from .store import users
        return {**store.api_usage(), 'shared_budget': settings.daily_budget_usd,
                'users': [{'id': u['id'], 'name': u['name'], 'is_admin': u['is_admin'],
                           'usage': store.api_usage(u['id'])} for u in store._all(select(users).limit(500))]}

    @app.put('/api/admin/usage/{user_id}')
    def set_allowance(user_id: str, body: AllowanceIn, user=Depends(require_admin), store: Store = Depends(get_store)):
        if not store.user(user_id):
            raise HTTPException(404, 'User not found')
        store.set_ai_profile(user_id, daily_limit=body.daily_limit)
        return store.api_usage(user_id)

    # ------------------------------------------------------------------ projects

    @app.get("/api/projects")
    def list_projects(user=Depends(require_user), store: Store = Depends(get_store)):
        return to_json(store.projects(user["id"]))

    @app.post("/api/projects")
    def create_project(body: ProjectIn, user=Depends(require_user), store: Store = Depends(get_store)):
        return to_json(store.create_project(user["id"], body.name, body.instructions))

    @app.patch("/api/projects/{project_id}")
    def patch_project(project_id: str, body: ProjectPatch, user=Depends(require_user),
                      store: Store = Depends(get_store)):
        project = store.update_project(user["id"], project_id, name=body.name, instructions=body.instructions)
        if not project:
            raise HTTPException(404, "Project not found")
        return to_json(project)

    @app.delete("/api/projects/{project_id}")
    def remove_project(project_id: str, user=Depends(require_user), store: Store = Depends(get_store)):
        store.delete_project(user["id"], project_id)
        return {"ok": True}

    # ------------------------------------------------------------------ chats

    @app.get("/api/chats")
    def list_chats(project_id: str | None = None, user=Depends(require_user), store: Store = Depends(get_store)):
        return to_json(store.chats(user["id"], project_id))

    @app.post("/api/chats")
    def create_chat(body: ChatIn, user=Depends(require_user), store: Store = Depends(get_store)):
        return to_json(store.create_chat(user["id"], body.project_id))

    @app.get("/api/chats/{chat_id}")
    def get_chat(chat_id: str, user=Depends(require_user), store: Store = Depends(get_store)):
        chat = store.chat(user["id"], chat_id)
        if not chat:
            raise HTTPException(404, "Chat not found")
        pending = store.pending_run(user["id"], chat_id)
        active = runner.runs.get(pending["id"]) if pending else None
        resumable = bool(active or (pending and pending["status"] == "paused"
                                    and settings.checkpoint_backend == "postgres"))
        if pending and not resumable:
            store.save_run(pending["id"], "interrupted", pending.get("events") or [],
                           {"reason": "The service restarted while a model call was active"})
        saved_events = (active.events if active else pending.get("events", [])) if pending else []
        since = max((i for i, e in enumerate(saved_events) if e["type"] in ("clarify", "answered")), default=0)
        return to_json({**chat, "messages": store.messages(chat_id), "pending_since": since,
                        "pending_run": pending["id"] if resumable else None,
                        "interrupted_run": bool(pending and not resumable)})

    @app.patch("/api/chats/{chat_id}")
    def patch_chat(chat_id: str, body: ChatPatch, user=Depends(require_user), store: Store = Depends(get_store)):
        chat = store.update_chat(user["id"], chat_id, title=body.title,
                                 project_id=body.project_id if body.move else False)
        if not chat:
            raise HTTPException(404, "Chat not found")
        return to_json(chat)

    @app.delete("/api/chats/{chat_id}")
    def remove_chat(chat_id: str, user=Depends(require_user), store: Store = Depends(get_store)):
        store.delete_chat(user["id"], chat_id)
        return {"ok": True}

    # ------------------------------------------------------------------ asking

    @app.post("/api/chats/{chat_id}/ask")
    def ask(chat_id: str, body: AskIn, user=Depends(require_user), store: Store = Depends(get_store)):
        chat = store.chat(user["id"], chat_id)
        if not chat:
            raise HTTPException(404, "Chat not found")
        with runner.admission_lock:
            run_id = hashlib.sha256(f"{user['id']}:{chat_id}:{body.idempotency_key}".encode()).hexdigest()[:32] \
                if body.idempotency_key else None
            saved = store.run_record(user["id"], run_id) if run_id and not body.pending_run_id else None
            if saved:
                if saved["question"] != body.question.strip():
                    raise HTTPException(409, "This idempotency key was used for a different request.")
                return {"run_id": run_id, "message_id": (saved.get("summary") or {}).get("message_id")}
            if body.pending_run_id:
                prior = store.run_record(user["id"], body.pending_run_id)
                if not prior or prior["chat_id"] != chat_id:
                    raise HTTPException(404, "Pending analysis not found in this chat")
                for i, event in enumerate(prior.get("events") or []):
                    if body.idempotency_key and event.get("response_id") == body.idempotency_key:
                        if (event.get("message") != body.question.strip()
                                or event.get("plan_version") != body.plan_version):
                            raise HTTPException(409, "This reply key was used for a different message.")
                        return {"run_id": prior["id"], "since": i, "message_id": event.get("message_id")}
            pending = store.pending_run(user["id"], chat_id)
            if pending:
                if body.pending_run_id != pending["id"]:
                    raise HTTPException(409, "Reply to the current pending analysis before starting another.")
                run = runner.restore(store, user, pending["id"])
                if not run:
                    raise HTTPException(409, "This analysis was interrupted. Reopen the chat before starting again.")
                since = runner.answer(store, run, body.question.strip(), body.plan_version, body.idempotency_key)
                return {"run_id": run.id, "since": since, "message_id": run.last_message_id}
            if body.pending_run_id or body.plan_version is not None:
                raise HTTPException(409, "This analysis is no longer waiting for a reply.")
            runner.prune()
            if sum(not r.finished for r in runner.runs.values()) >= settings.max_pending_runs:
                raise HTTPException(429, "The analysis queue is full. Please retry later.")
            if any(r.chat["id"] == chat_id and not r.finished for r in runner.runs.values()):
                raise HTTPException(409, "This chat is already answering a question.")
            if not store.reserve_question(user["id"], None if user["is_admin"] else settings.daily_question_limit):
                raise HTTPException(429, f"You have used today's {settings.daily_question_limit} live questions. "
                                         "Demo replays are still available.")
            run = runner.start(store, user, chat, body.question.strip(), run_id=run_id)
            return {"run_id": run.id, "message_id": run.message_id}

    @app.post("/api/chats/{chat_id}/messages/{message_id}/edit")
    def edit_message(chat_id: str, message_id: str, body: EditMessageIn,
                     user=Depends(require_user), store: Store = Depends(get_store)):
        if not store.chat(user["id"], chat_id):
            raise HTTPException(404, "Chat not found")
        content = body.content.strip()
        if not content:
            raise HTTPException(422, "Enter a message before saving")
        with runner.admission_lock:
            rows = store.messages(chat_id)
            index = next((i for i, m in enumerate(rows) if m['id'] == message_id and m['role'] == 'user'), None)
            if index is None:
                raise HTTPException(404, "Your message was not found")
            message = rows[index]
            pending = store.pending_run(user['id'], chat_id)
            # Replaying a successful in-place edit must not become a new fork after the run finishes.
            if body.pending_run_id:
                old = store.run_record(user['id'], body.pending_run_id)
                if old and old['chat_id'] == chat_id:
                    for i, event in enumerate(old.get('events') or []):
                        if event.get('response_id') == body.idempotency_key:
                            if (event.get('edited_message_id') != message_id or event.get('message') != content
                                    or event.get('plan_version') != body.plan_version):
                                raise HTTPException(409, 'This edit key was used for a different message')
                            return {'chat_id': chat_id, 'run_id': old['id'], 'since': i, 'mode': 'reply'}
            if pending and pending['status'] == 'running':
                raise HTTPException(409, 'Wait for the current response before editing a message.')
            latest = next((m['id'] for m in reversed(rows) if m['role'] == 'user'), None)
            reply = ((message.get('run') or {}).get('kind') == 'reply' or
                     index > 0 and (rows[index - 1].get('run') or {}).get('type') == 'clarify')
            if pending and reply:
                events = pending.get('events') or []
                answered = next((i for i in range(len(events) - 1, -1, -1) if events[i].get('type') == 'answered'
                    and (events[i].get('message_id') == message_id or
                         not events[i].get('message_id') and events[i].get('message') == message['content'])), None)
                # Approvals that already produced results are immutable evidence: edit them in a new copy.
                if answered is None or any(e.get('type') == 'result' or
                    e.get('step') == 'execute' and e.get('status') == 'done' for e in events[answered + 1:]):
                    reply = False
            if body.pending_run_id and latest == message_id and reply and not pending:
                raise HTTPException(409, 'The analysis finished. Reopen the chat before editing this reply.')
            if pending and latest == message_id and reply:
                if body.pending_run_id != pending['id'] or body.plan_version is None:
                    raise HTTPException(409, 'Reopen the chat to edit the latest pending reply.')
                run = runner.restore(store, user, pending['id'])
                if not run:
                    raise HTTPException(409, 'The paused analysis is unavailable. Reopen the chat first.')
                since = runner.answer(store, run, content, body.plan_version, body.idempotency_key,
                                      edit={'id': message_id, 'expected': body.expected_content})
                return {'chat_id': chat_id, 'run_id': run.id, 'since': since, 'mode': 'reply'}
            try:
                fork = store.fork_message(user['id'], chat_id, message_id, content,
                                          body.expected_content, body.idempotency_key)
            except LookupError as error:
                raise HTTPException(404, str(error)) from error
            except ValueError as error:
                raise HTTPException(409, str(error)) from error
            run_id = hashlib.sha256(f"edit:{fork['chat_id']}".encode()).hexdigest()[:32]
            if store.run_record(user['id'], run_id):
                return {'chat_id': fork['chat_id'], 'run_id': run_id, 'mode': 'fork'}
            runner.prune()
            if sum(not r.finished for r in runner.runs.values()) >= settings.max_pending_runs:
                raise HTTPException(429, 'The analysis queue is full. Retry saving this edit shortly.')
            if not store.reserve_question(user['id'], None if user['is_admin'] else settings.daily_question_limit):
                raise HTTPException(429, 'Your daily question allowance is used. The edited copy is saved.')
            run = runner.start(store, user, store.chat(user['id'], fork['chat_id']), fork['question'],
                               run_id=run_id, initial_answers=fork['answers'], recorded_message_id=fork['message_id'])
            return {'chat_id': fork['chat_id'], 'run_id': run.id, 'mode': 'fork'}

    @app.post("/api/runs/{run_id}/answer")
    def answer(run_id: str, body: AnswerIn, user=Depends(require_user), store: Store = Depends(get_store)):
        with runner.admission_lock:
            run = runner.restore(store, user, run_id)
            if not run or run.user["id"] != user["id"]:
                raise HTTPException(404, "Run not found")
            since = runner.answer(store, run, body.message.strip(), body.plan_version, body.idempotency_key)
            return {"ok": True, "run_id": run.id, "since": since}

    @app.get("/api/runs/{run_id}/stream")
    async def stream(run_id: str, request: Request, since: int = Query(0, ge=0),
                     user=Depends(require_user), store: Store = Depends(get_store)):
        run = runner.restore(store, user, run_id)
        if not run or run.user["id"] != user["id"]:
            raise HTTPException(404, "Run not found")

        async def events():
            index = since
            heartbeat = time.monotonic()
            while True:
                while index < len(run.events):
                    event = run.events[index]
                    index += 1
                    yield f"id: {index}\ndata: {json.dumps(to_json(event))}\n\n"
                    if event["type"] in ("clarify", "final"):
                        return
                if await request.is_disconnected():
                    return
                if time.monotonic() - heartbeat >= 12:
                    # SSE comments are ignored by EventSource but prevent idle proxy timeouts.
                    yield ": heartbeat\n\n"
                    heartbeat = time.monotonic()
                await asyncio.sleep(0.15)
        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/runs/{run_id}/progress")
    async def progress(run_id: str, request: Request, since: int = Query(0, ge=0),
                       user=Depends(require_user), store: Store = Depends(get_store)):
        """Deliver the next events in a short JSON request when the browser loses its live stream."""
        run = runner.runs.get(run_id)
        if run is not None:
            if run.user["id"] != user["id"]:
                raise HTTPException(404, "Run not found")
            deadline = time.monotonic() + 5
            while since == len(run.events) and not (run.finished or run.paused):
                if time.monotonic() >= deadline or await request.is_disconnected():
                    break
                await asyncio.sleep(0.2)
            events = run.events[since:]
            can_continue = not (run.finished or run.paused)
        else:
            saved = await run_in_threadpool(store.run_record, user["id"], run_id)
            if not saved:
                raise HTTPException(404, "Run not found")
            events = (saved.get("events") or [])[since:]
            if saved["status"] in ("running", "interrupted") or (
                saved["status"] == "paused" and settings.checkpoint_backend != "postgres"
            ):
                events = []
            can_continue = False

        batch = []
        for event in events:
            batch.append(event)
            if event["type"] in ("clarify", "final"):
                break
        return JSONResponse(to_json({"events": batch, "active": can_continue}),
                            headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------------ feedback and operational metrics
    @app.post("/api/feedback")
    def submit_feedback(body: FeedbackIn, user=Depends(require_user), store: Store = Depends(get_store)):
        try:
            store.add_feedback(user["id"], body.chat_id, body.run_id, body.rating,
                               body.category, body.comment)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"ok": True}

    @app.get("/api/admin/feedback")
    def export_feedback(user=Depends(require_admin), store: Store = Depends(get_store)):
        return {"feedback": store.feedback_export()}

    @app.get("/api/admin/metrics")
    def run_metrics(user=Depends(require_admin), store: Store = Depends(get_store)):
        return store.run_metrics()

    # ------------------------------------------------------------------ memory

    @app.get("/api/memory")
    def memory(user=Depends(require_user), store: Store = Depends(get_store)):
        size = settings.data_size
        projects = {p["id"]: p["name"] for p in store.projects(user["id"])}
        mine = store.memories(user_id=user["id"], data_size=size)
        return to_json({
            "preferences": user.get("preferences") or "",
            "insights": [{**m, "project_name": projects.get(m["project_id"])}
                         for m in mine if m["scope"] != "business"],
            "business": store.memories(scope="business", data_size=size),
            "is_admin": bool(user["is_admin"]),
        })

    @app.put("/api/memory/preferences")
    def set_preferences(body: PreferencesIn, user=Depends(require_user), store: Store = Depends(get_store)):
        store.set_preferences(user["id"], body.text)
        return {"ok": True}

    @app.delete("/api/memory/{memory_id}")
    def forget(memory_id: str, user=Depends(require_user), store: Store = Depends(get_store)):
        store.delete_memory(user["id"], memory_id, bool(user["is_admin"]))
        return {"ok": True}

    @app.post("/api/memory/{memory_id}/promote")
    def promote(memory_id: str, user=Depends(require_admin), store: Store = Depends(get_store)):
        store.promote_memory(memory_id)
        return {"ok": True}

    # ------------------------------------------------------------------ pulse
    @app.get("/api/pulse")
    def pulse(store: Store = Depends(get_store)):
        path = settings.memory_dir / "kpis" / "latest_pulse.json"
        report = json.loads(path.read_text()) if path.exists() else {"flags": []}
        resolved = store.resolved_gaps()
        flags = [{**f, "resolved": (f["id"], f["window_end"]) in resolved} for f in report.get("flags", [])]
        return {"data_as_of": report.get("data_as_of"), "series": report.get("series"), "flags": flags}

    # ------------------------------------------------------------------ demos
    @app.get("/api/demos")
    def list_demos():
        return demo_runs.demos()

    @app.get("/api/demos/{name}/stream")
    async def replay(name: str, request: Request, instant: bool = False):
        demo = demo_runs.load(name)
        if not demo:
            raise HTTPException(404, "Demo not found")

        async def events():
            for delay, event in demo_runs.replay(demo):
                if delay and not instant:
                    await asyncio.sleep(delay)
                if await request.is_disconnected():
                    return
                yield f"data: {json.dumps(event)}\n\n"
        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app


def main() -> None:
    import os

    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run("pantree_insights.web:create_app", factory=True, host=os.environ.get("HOST", "127.0.0.1"),
                port=int(os.environ.get("PORT", "8000")), proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
