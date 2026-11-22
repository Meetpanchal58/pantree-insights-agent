"""Register routes data endpoints with the app-owned web.runner."""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import demos as demo_runs
from .. import explorer
from ..auth import require_admin, require_user
from ..store import Store, get_store, to_json
from .models import (
    FeedbackIn,
    PreferencesIn,
)

log = logging.getLogger("pantree.web")

def register(app, settings):

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

    @app.get("/api/pulse")
    def pulse(store: Store = Depends(get_store)):
        path = settings.memory_dir / "kpis" / "latest_pulse.json"
        report = json.loads(path.read_text()) if path.exists() else {"flags": []}
        resolved = store.resolved_gaps()
        flags = [{**f, "resolved": (f["id"], f["window_end"]) in resolved} for f in report.get("flags", [])]
        return {"data_as_of": report.get("data_as_of"), "series": report.get("series"), "flags": flags}

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
