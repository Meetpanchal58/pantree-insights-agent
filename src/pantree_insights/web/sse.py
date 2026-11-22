"""Register sse endpoints with the app-owned web.runner."""
from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi import Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse

from ..auth import require_user
from ..store import Store, get_store, to_json

log = logging.getLogger("pantree.web")

def register(app, settings):
    from .. import web

    @app.get("/api/runs/{run_id}/stream")
    async def stream(run_id: str, request: Request, since: int = Query(0, ge=0),
                     user=Depends(require_user), store: Store = Depends(get_store)):
        run = web.runner.restore(store, user, run_id)
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
        run = web.runner.runs.get(run_id)
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
