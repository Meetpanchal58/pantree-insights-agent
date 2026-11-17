"""The web app (FastAPI): chats and projects, live agent runs over Server-Sent Events, memory, pulse, demos.

    pantree-web                      # http://127.0.0.1:8000

A question runs in a background thread; its progress events are kept in memory and streamed to the
browser (GET /api/runs/{id}/stream). Clarifying questions pause the run; POST /api/runs/{id}/answer
resumes it. Finished runs are saved as chat messages (with their events, so they can be replayed).
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from . import demos as demo_runs
from .auth import current_user, methods, require_admin, require_user
from .auth import router as auth_router
from .config import get_settings
from .db import manifest, snapshot_signature
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
    use_defaults: bool = False


class AnswerIn(BaseModel):
    answers: dict[str, str] = {}


class FeedbackIn(BaseModel):
    chat_id: str = Field(min_length=1, max_length=32)
    run_id: str = Field(min_length=1, max_length=32)
    rating: str = Field(pattern="^(positive|negative)$")
    category: str = Field("other", max_length=40)
    comment: str = Field("", max_length=1000)


class PreferencesIn(BaseModel):
    text: str = Field("", max_length=4000)


# ----------------------------------------------------------------------------- background runs
@dataclass
class Run:
    id: str
    user: dict
    chat: dict
    question: str
    started: float = field(default_factory=time.time)
    events: list[dict] = field(default_factory=list)
    paused: bool = False              # waiting for clarifying answers
    finished: bool = False
    segment_done: threading.Event = field(default_factory=threading.Event)
    agent: object | None = None


def failure_details(error: Exception) -> dict:
    """User-facing failures without raw provider/credential details."""
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
        self.runs: dict[str, Run] = {}

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

    def start(self, store: Store, user: dict, chat: dict, question: str, use_defaults: bool) -> Run:
        # Initialize the agent before writing a user question: a cold-start error should
        # not leave behind a message that can never receive an analysis result.
        agent = self.agent
        history = store.history(chat["id"])
        store.add_message(chat["id"], "user", question)
        if chat["title"] == "New chat":
            store.update_chat(user["id"], chat["id"], title=question[:80])
        scope = MemoryScope(store, user, chat)
        run_id, events = agent.start(question, chat_id=chat["id"], history=history,
                                          use_defaults=use_defaults, memory=scope)
        run = Run(run_id, user, chat, question, agent=agent)
        self.runs[run_id] = run
        store.create_run(run_id, user["id"], chat["id"], question, snapshot=json.dumps(manifest(), sort_keys=True))
        self._consume(store, run, events)
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
        if old_snapshot and old_snapshot != json.dumps(manifest(), sort_keys=True):
            store.save_run(run_id, "interrupted", saved.get("events") or [],
                           {"reason": "The dataset changed during a paused analysis"})
            return None
        chat = store.chat(user["id"], saved["chat_id"])
        if chat is None:
            return None
        agent = self.agent
        run = Run(run_id, user, chat, saved["question"], agent=agent)
        run.events = saved["events"] or []
        run.started = min((e.get("t", time.time()) for e in run.events), default=time.time())
        run.paused = True
        agent.restore_memory(run_id, MemoryScope(store, user, chat))
        self.runs[run_id] = run
        return run

    def answer(self, store: Store, run: Run, answers: dict[str, str]) -> None:
        run.paused = False
        run.segment_done.clear()
        run.events.append({"type": "answered", "answers": answers, "t": time.time()})
        if store is not None:
            store.save_run(run.id, "running", run.events)
        self._consume(store, run, (run.agent or self.agent).resume(run.id, answers))

    def _consume(self, store: Store, run: Run, events) -> None:
        def work():
            try:
                for event in events:
                    run.events.append({**event, "t": event.get("t", time.time())})
                    if event["type"] == "clarify":
                        run.paused = True
                        store.save_run(run.id, "paused", run.events)
                    elif event["type"] == "final":
                        self._save(store, run, event)
            except Exception as error:                      # never leave the browser waiting
                log.exception("run %s failed", run.id)
                final = {"type": "final", "run_id": run.id, "question": run.question,
                         "status": "failed", "t": time.time(),
                         **failure_details(error)}
                run.events.append(final)
                self._save(store, run, final)
            finally:
                run.segment_done.set()
        threading.Thread(target=work, daemon=True, name=f"run-{run.id}").start()

    def _save(self, store: Store, run: Run, final: dict) -> None:
        run.finished = True
        headline = ((final.get("analysis") or {}).get("headline") or final.get("message") or "")
        timeline = [{**e, "t": round(e["t"] - run.started, 2)} for e in run.events if e["type"] != "final"]
        stage_times = {}
        for event in run.events:
            if event.get("type") == "step" and event.get("status") != "started":
                detail = event.get("detail") or {}
                if isinstance(detail.get("seconds"), (float, int)):
                    stage_times[event["step"]] = round(stage_times.get(event["step"], 0) + detail["seconds"], 2)
        calls = final.get("calls") or []
        store.save_run(run.id, final.get("status", "failed"), run.events,
                       {"stages": stage_times, "cost_usd": (final.get("cost") or {}).get("usd", 0),
                        "llm_calls": len(calls), "sql_attempts": final.get("attempts", 0),
                        "verification": (final.get("verification") or {}).get("status")})
        store.add_message(run.chat["id"], "assistant", headline, to_json({**final, "events": timeline}))
        if final.get("cost"):
            store.add_cost(run.user["id"], final["cost"].get("usd", 0.0))
        run.agent = None  # Finished runs no longer need to retain an older snapshot's entire agent/index.


runner = Runner()


# ----------------------------------------------------------------------------- app
@asynccontextmanager
async def lifespan(app: FastAPI):
    def warm():
        try:
            from .pulse import run as pulse
            pulse()                                           # KPI history + open gaps for this data
        except Exception:
            log.exception("pulse failed at startup")
        try:
            _ = runner.agent                                  # build indexes before the first question
        except Exception:
            log.exception("agent failed to start")
    threading.Thread(target=warm, daemon=True).start()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    if settings.app_env == "production" and settings.session_secret.startswith("dev-only"):
        raise RuntimeError("Set SESSION_SECRET in production.")
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
                          "unlimited": bool(user["is_admin"])}}

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
        return to_json({**chat, "messages": store.messages(chat_id),
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
        if any(r.chat["id"] == chat_id and not r.finished for r in runner.runs.values()):
            raise HTTPException(409, "This chat is already answering a question.")
        if not user["is_admin"] and store.questions_today(user["id"]) >= settings.daily_question_limit:
            raise HTTPException(429, f"You have used today's {settings.daily_question_limit} live questions. "
                                     "Demo replays are still available.")
        store.count_question(user["id"])
        run = runner.start(store, user, chat, body.question.strip(), body.use_defaults)
        return {"run_id": run.id}

    @app.post("/api/runs/{run_id}/answer")
    def answer(run_id: str, body: AnswerIn, user=Depends(require_user), store: Store = Depends(get_store)):
        run = runner.restore(store, user, run_id)
        if not run or run.user["id"] != user["id"]:
            raise HTTPException(404, "Run not found")
        if not run.paused:
            raise HTTPException(409, "This run is not waiting for answers.")
        runner.answer(store, run, body.answers)
        return {"ok": True}

    @app.get("/api/runs/{run_id}/stream")
    async def stream(run_id: str, request: Request, since: int = 0,
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
