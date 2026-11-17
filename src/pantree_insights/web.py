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
from .db import snapshot_signature
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


class PreferencesIn(BaseModel):
    text: str = Field("", max_length=4000)


class FeedbackIn(BaseModel):
    rating: str = Field(pattern="^(up|down)$")
    reason: str = Field("", max_length=40)
    note: str = Field("", max_length=1000)


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
    """Owns active workers while durable graph/run state lives in Postgres."""

    def __init__(self, agent_factory=None):
        self._agent_factory = agent_factory
        self._agent = None
        self._snapshot = None
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(max(1, get_settings().max_concurrent_runs))
        self.runs: dict[str, Run] = {}

    @property
    def agent(self):
        with self._lock:
            signature = snapshot_signature() if self._agent_factory is None else None
            if self._agent is None or signature != self._snapshot:
                from .graph import Agent
                self._agent = (self._agent_factory or Agent)()
                self._snapshot = signature
            return self._agent

    def start(self, store: Store, user: dict, chat: dict, question: str, use_defaults: bool) -> Run:
        history = store.history(chat["id"])
        store.add_message(chat["id"], "user", question)
        if chat["title"] == "New chat":
            store.update_chat(user["id"], chat["id"], title=question[:80])
        scope = MemoryScope(store, user, chat)
        agent = self.agent
        run_id, events = agent.start(question, chat_id=chat["id"], history=history,
                                          use_defaults=use_defaults, memory=scope)
        run = Run(run_id, user, chat, question, agent=agent)
        self.runs[run_id] = run
        store.create_run(run_id, user["id"], chat["id"], question)
        self._consume(store, run, events)
        return run

    def restore(self, store: Store, user: dict, run_id: str) -> Run | None:
        saved = store.run(user["id"], run_id)
        if not saved:
            return None
        chat = store.chat(user["id"], saved["chat_id"])
        if not chat:
            return None
        run = Run(run_id, user, chat, saved["question"], events=list(saved.get("events") or []),
                  paused=saved["status"] == "paused", finished=saved["status"] == "finished", agent=self.agent)
        self.runs[run_id] = run
        run.agent.attach_memory(run_id, MemoryScope(store, user, chat))
        if saved["status"] in ("running", "queued"):
            self._consume(store, run, run.agent.continue_run(run_id))
        return run

    def answer(self, store: Store, run: Run, answers: dict[str, str]) -> None:
        run.paused = False
        run.segment_done.clear()
        run.events.append({"type": "answered", "answers": answers, "t": time.time()})
        self._consume(store, run, (run.agent or self.agent).resume(run.id, answers))

    def _consume(self, store: Store, run: Run, events) -> None:
        def work():
            try:
                for event in events:
                    run.events.append({**event, "t": event.get("t", time.time())})
                    if event["type"] == "clarify":
                        run.paused = True
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
        from .db import manifest
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
        pending = [r.id for r in runner.runs.values() if r.chat["id"] == chat_id and not r.finished]
        return to_json({**chat, "messages": store.messages(chat_id), "pending_run": pending[-1] if pending else None})

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
        run = runner.runs.get(run_id)
        if not run or run.user["id"] != user["id"]:
            raise HTTPException(404, "Run not found")
        if not run.paused:
            raise HTTPException(409, "This run is not waiting for answers.")
        runner.answer(store, run, body.answers)
        return {"ok": True}

    @app.get("/api/runs/{run_id}/stream")
    async def stream(run_id: str, request: Request, since: int = 0, user=Depends(require_user)):
        run = runner.runs.get(run_id)
        if not run or run.user["id"] != user["id"]:
            raise HTTPException(404, "Run not found")

        async def events():
            index = since
            while True:
                while index < len(run.events):
                    event = run.events[index]
                    index += 1
                    yield f"id: {index}\ndata: {json.dumps(to_json(event))}\n\n"
                    if event["type"] in ("clarify", "final"):
                        return
                if await request.is_disconnected():
                    return
                await asyncio.sleep(0.15)
        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

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
