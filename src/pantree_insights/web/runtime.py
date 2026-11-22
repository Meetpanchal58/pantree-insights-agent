"""Web: runner."""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field

from fastapi import HTTPException

from .. import providers
from ..config import get_settings
from ..db import snapshot_signature
from ..llm import BudgetExceeded, ChatLLM
from ..store import MemoryScope, Store

log = logging.getLogger("pantree.web")

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
                from ..graph import Agent
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
