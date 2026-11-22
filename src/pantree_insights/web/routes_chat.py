"""Register routes chat endpoints with the app-owned web.runner."""
from __future__ import annotations

import hashlib
import logging

from fastapi import Depends, HTTPException

from ..auth import require_user
from ..store import Store, get_store, to_json
from .models import (
    AnswerIn,
    AskIn,
    ChatIn,
    ChatPatch,
    EditMessageIn,
    ProjectIn,
    ProjectPatch,
)

log = logging.getLogger("pantree.web")

def register(app, settings):
    from .. import web

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
        active = web.runner.runs.get(pending["id"]) if pending else None
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

    @app.post("/api/chats/{chat_id}/ask")
    def ask(chat_id: str, body: AskIn, user=Depends(require_user), store: Store = Depends(get_store)):
        chat = store.chat(user["id"], chat_id)
        if not chat:
            raise HTTPException(404, "Chat not found")
        with web.runner.admission_lock:
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
                run = web.runner.restore(store, user, pending["id"])
                if not run:
                    raise HTTPException(409, "This analysis was interrupted. Reopen the chat before starting again.")
                since = web.runner.answer(store, run, body.question.strip(), body.plan_version, body.idempotency_key)
                return {"run_id": run.id, "since": since, "message_id": run.last_message_id}
            if body.pending_run_id or body.plan_version is not None:
                raise HTTPException(409, "This analysis is no longer waiting for a reply.")
            web.runner.prune()
            if sum(not r.finished for r in web.runner.runs.values()) >= settings.max_pending_runs:
                raise HTTPException(429, "The analysis queue is full. Please retry later.")
            if any(r.chat["id"] == chat_id and not r.finished for r in web.runner.runs.values()):
                raise HTTPException(409, "This chat is already answering a question.")
            if not store.reserve_question(user["id"], None if user["is_admin"] else settings.daily_question_limit):
                raise HTTPException(429, f"You have used today's {settings.daily_question_limit} live questions. "
                                         "Demo replays are still available.")
            run = web.runner.start(store, user, chat, body.question.strip(), run_id=run_id)
            return {"run_id": run.id, "message_id": run.message_id}

    @app.post("/api/chats/{chat_id}/messages/{message_id}/edit")
    def edit_message(chat_id: str, message_id: str, body: EditMessageIn,
                     user=Depends(require_user), store: Store = Depends(get_store)):
        if not store.chat(user["id"], chat_id):
            raise HTTPException(404, "Chat not found")
        content = body.content.strip()
        if not content:
            raise HTTPException(422, "Enter a message before saving")
        with web.runner.admission_lock:
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
                run = web.runner.restore(store, user, pending['id'])
                if not run:
                    raise HTTPException(409, 'The paused analysis is unavailable. Reopen the chat first.')
                since = web.runner.answer(store, run, content, body.plan_version, body.idempotency_key,
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
            web.runner.prune()
            if sum(not r.finished for r in web.runner.runs.values()) >= settings.max_pending_runs:
                raise HTTPException(429, 'The analysis queue is full. Retry saving this edit shortly.')
            if not store.reserve_question(user['id'], None if user['is_admin'] else settings.daily_question_limit):
                raise HTTPException(429, 'Your daily question allowance is used. The edited copy is saved.')
            run = web.runner.start(store, user, store.chat(user['id'], fork['chat_id']), fork['question'],
                               run_id=run_id, initial_answers=fork['answers'], recorded_message_id=fork['message_id'])
            return {'chat_id': fork['chat_id'], 'run_id': run.id, 'mode': 'fork'}

    @app.post("/api/runs/{run_id}/answer")
    def answer(run_id: str, body: AnswerIn, user=Depends(require_user), store: Store = Depends(get_store)):
        with web.runner.admission_lock:
            run = web.runner.restore(store, user, run_id)
            if not run or run.user["id"] != user["id"]:
                raise HTTPException(404, "Run not found")
            since = web.runner.answer(store, run, body.message.strip(), body.plan_version, body.idempotency_key)
            return {"ok": True, "run_id": run.id, "since": since}
