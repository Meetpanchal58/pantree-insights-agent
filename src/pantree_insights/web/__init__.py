"""The web app (FastAPI): chats and projects, live agent runs over Server-Sent Events, memory, pulse, demos.

    pantree-web                      # http://127.0.0.1:8000

A question runs in a background thread; its progress events are kept in memory and streamed to the
browser (GET /api/runs/{id}/stream). Clarifying questions pause the run; POST /api/runs/{id}/answer
resumes it. Finished runs are saved as chat messages (with their events, so they can be replayed).
"""
from __future__ import annotations

import asyncio as asyncio
import logging
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .. import demos as demo_runs  # noqa: F401
from .. import providers
from ..auth import require_user
from ..auth import router as auth_router
from ..config import get_settings
from . import routes_account, routes_chat, routes_data, sse
from .models import AISettingsIn as AISettingsIn
from .models import AllowanceIn as AllowanceIn
from .models import AnswerIn as AnswerIn
from .models import AskIn as AskIn
from .models import ChatIn as ChatIn
from .models import ChatPatch as ChatPatch
from .models import ConnectionIn as ConnectionIn
from .models import EditMessageIn as EditMessageIn
from .models import FeedbackIn as FeedbackIn
from .models import PreferencesIn as PreferencesIn
from .models import ProjectIn as ProjectIn
from .models import ProjectPatch as ProjectPatch
from .runtime import Run as Run
from .runtime import Runner as Runner
from .runtime import failure_details as failure_details

log = logging.getLogger("pantree.web")
STATIC = Path(__file__).resolve().parent.parent / "static"

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

    # ------------------------------------------------------------------ dataset browsing (no LLM or quota)


    # ------------------------------------------------------------------ AI settings and accounting


    # ------------------------------------------------------------------ projects


    # ------------------------------------------------------------------ chats


    # ------------------------------------------------------------------ asking


    # ------------------------------------------------------------------ feedback and operational metrics


    # ------------------------------------------------------------------ memory


    # ------------------------------------------------------------------ pulse

    # ------------------------------------------------------------------ demos


    routes_account.register(app, settings)
    routes_chat.register(app, settings)
    sse.register(app, settings)
    routes_data.register(app, settings)
    return app


def main() -> None:
    import os

    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run("pantree_insights.web:create_app", factory=True, host=os.environ.get("HOST", "127.0.0.1"),
                port=int(os.environ.get("PORT", "8000")), proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
