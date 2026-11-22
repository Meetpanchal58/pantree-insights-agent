"""Store: runs."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from sqlalchemy import (
    insert,
    select,
    update,
)

from .models import _id, _now, chats, feedback, investigations, messages, run_records, usage
from .serialization import to_json


class RunStore:
    def create_run(self, run_id: str, user_id: str, chat_id: str, question: str,
                   snapshot: str | None = None, use_defaults: bool = False, ai: dict | None = None,
                   message_id: str | None = None) -> None:
        self._exec(insert(run_records).values(id=run_id, user_id=user_id, chat_id=chat_id,
                                               question=question, status="running", events=[],
                                               summary={"snapshot": snapshot, "use_defaults": use_defaults,
                                                        "message_id": message_id,
                                                        **({"ai": ai} if ai else {})}))

    def save_run(self, run_id: str, status: str, events: list[dict], summary: dict | None = None) -> None:
        values = {"status": status, "events": to_json(events), "updated_at": _now()}
        if summary is not None:
            values["summary"] = to_json(summary)
        self._exec(update(run_records).where(run_records.c.id == run_id).values(**values))

    def save_pause(self, run_id: str, chat_id: str, events: list[dict], event: dict) -> None:
        """The assistant's proposal and paused run become visible together; replay cannot duplicate messages."""
        mid = hashlib.sha256(f"{run_id}:{event.get('plan_version')}:{event.get('message')}".encode()).hexdigest()[:16]
        with self.engine.begin() as c:
            c.execute(update(run_records).where(run_records.c.id == run_id)
                      .values(status="paused", events=to_json(events), updated_at=_now()))
            c.execute(self._upsert(messages).values(id=mid, chat_id=chat_id, role="assistant",
                      content=event.get("message", ""), run=to_json(event)).on_conflict_do_nothing())

    def save_reply(self, run_id: str, chat_id: str, events: list[dict], message: str,
                   edit: dict | None = None) -> str:
        mid = edit['id'] if edit else _id()
        events[-1]['message_id'] = mid
        with self.engine.begin() as c:
            changed = c.execute(update(run_records).where(run_records.c.id == run_id,
                                run_records.c.chat_id == chat_id, run_records.c.status == "paused")
                                .values(status="running", events=to_json(events), updated_at=_now()))
            if not changed.rowcount:
                raise ValueError("This proposal has already been answered")
            info = {'kind': 'reply', 'run_id': run_id, 'plan_version': events[-1].get('plan_version')}
            if edit:
                prior = c.execute(select(messages).where(messages.c.id == mid, messages.c.chat_id == chat_id,
                                  messages.c.role == 'user')).mappings().first()
                if not prior or prior['content'] != edit['expected']:
                    raise ValueError('This message changed. Reopen the chat before editing it.')
                info = {**(prior['run'] or {}), **info}
                info['edits'] = [*(prior['run'] or {}).get('edits', []),
                                 {'content': prior['content'], 'edited_at': str(_now())}]
                c.execute(update(messages).where(messages.c.id == mid).values(content=message, run=to_json(info)))
                later = c.execute(select(messages).where(messages.c.chat_id == chat_id,
                    messages.c.role == 'assistant', messages.c.created_at > prior['created_at'])).mappings().all()
                for old in later:
                    payload = old['run'] or {}
                    if payload.get('type') == 'clarify':
                        c.execute(update(messages).where(messages.c.id == old['id']).values(
                            run=to_json({**payload, 'superseded_by_edit': mid})))
            else:
                c.execute(insert(messages).values(id=mid, chat_id=chat_id, role="user", content=message, run=info))
        return mid

    def complete_run(self, run_id: str, final: dict, events: list[dict], summary: dict,
                     headline: str, message: dict) -> bool:
        """Commit terminal state, answer and spend once, atomically, including concurrent retries."""
        with self.engine.begin() as c:
            original = c.execute(select(run_records.c.summary).where(run_records.c.id == run_id)).scalar()
            summary = {**(original or {}), **summary}
            row = c.execute(update(run_records).where(run_records.c.id == run_id,
                            run_records.c.status.in_(["running", "paused"]))
                .values(status=final.get("status", "failed"), events=to_json(events),
                        summary=to_json(summary), updated_at=_now())
                .returning(run_records.c.user_id, run_records.c.chat_id)).mappings().first()
            if row is None:
                return False
            c.execute(insert(messages).values(id=_id(), chat_id=row["chat_id"], role="assistant",
                                              content=headline, run=to_json(message)))
            if (final.get("status") == "done" and final.get("grounded") and final.get("confirmed_plan")
                    and not any(r["result"].get("truncated") for r in final.get("analyses", []))):
                prior = c.execute(select(investigations.c.context)
                                  .where(investigations.c.chat_id == row["chat_id"])).scalar() or {}
                if final.get("relationship") == "new":
                    prior = {}
                compact_plan = [{k: v for k, v in item.items() if k != "sql"}
                                for item in final["confirmed_plan"]]
                evidence = {"run_id": run_id, "question": final.get("question"),
                            "plan": compact_plan, "finding": headline,
                            "data_as_of": final.get("data_as_of"), "snapshot": summary.get("snapshot"),
                            "results": [{"purpose": r.get("purpose"), "columns": r["result"]["columns"],
                                         "data": r["result"]["data"][:6], "row_count": len(r["result"]["data"]),
                                         "excerpted": len(r["result"]["data"]) > 6,
                                         "findings": r.get("analysis", {}).get("findings", []),
                                         "truncated": r["result"].get("truncated")}
                                        for r in final.get("analyses", [])],
                            "next_questions": (final.get("analysis") or {}).get("follow_up_questions", [])}
                context = {"objective": final.get("objective") or prior.get("objective") or final.get("question"),
                           "origin_plan": prior.get("origin_plan") or compact_plan,
                           "current_plan": compact_plan, "populations": final.get("populations", []),
                           "metric_definitions": final.get("metric_definitions", {}),
                           "evidence": [*(prior.get("evidence") or []), evidence][-8:]}
                c.execute(self._upsert(investigations).values(chat_id=row["chat_id"], context=to_json(context))
                          .on_conflict_do_update(index_elements=[investigations.c.chat_id],
                                                set_={"context": to_json(context), "updated_at": _now()}))
            elif final.get("result") is not None or final.get("analyses"):
                # A newly displayed partial/unverified result supersedes the referent of "these".
                # Keep historical evidence, but never silently bind an older complete population.
                prior = c.execute(select(investigations.c.context)
                                  .where(investigations.c.chat_id == row["chat_id"])).scalar() or {}
                context = {**prior, "populations": []}
                c.execute(self._upsert(investigations).values(chat_id=row["chat_id"], context=to_json(context))
                          .on_conflict_do_update(index_elements=[investigations.c.chat_id],
                                                set_={"context": to_json(context), "updated_at": _now()}))
            c.execute(update(chats).where(chats.c.id == row["chat_id"]).values(updated_at=_now()))
            usd = 0.0 if summary.get("ai") else (final.get("cost") or {}).get("usd", 0.0)
            c.execute(self._upsert(usage).values(user_id=row["user_id"], day=str(datetime.now(UTC).date()),
                                               questions=0, usd=usd)
                .on_conflict_do_update(index_elements=[usage.c.user_id, usage.c.day], set_={"usd": usage.c.usd + usd}))
            return True

    def run_record(self, user_id: str, run_id: str) -> dict | None:
        return self._one(select(run_records).where(run_records.c.id == run_id,
                                                      run_records.c.user_id == user_id))

    def pending_run(self, user_id: str, chat_id: str) -> dict | None:
        return self._one(select(run_records).where(run_records.c.user_id == user_id,
                                                      run_records.c.chat_id == chat_id,
                                                      run_records.c.status.in_(["running", "paused"]))
                         .order_by(run_records.c.started_at.desc()).limit(1))

    def add_feedback(self, user_id: str, chat_id: str, run_id: str, rating: str,
                     category: str, comment: str) -> None:
        chat = self.chat(user_id, chat_id)
        if not chat:
            raise ValueError("Chat not found")
        valid = any((msg.get("run") or {}).get("run_id") == run_id
                    for msg in self.messages(chat_id) if msg["role"] == "assistant")
        if not valid:
            raise ValueError("Completed answer not found in this chat")
        existing = self._one(select(feedback).where(feedback.c.user_id == user_id,
                                                     feedback.c.run_id == run_id))
        if existing:
            self._exec(update(feedback).where(feedback.c.id == existing["id"])
                       .values(rating=rating, category=category, comment=comment))
        else:
            self._exec(insert(feedback).values(id=_id(), user_id=user_id, chat_id=chat_id,
                                                      run_id=run_id, rating=rating,
                                                      category=category, comment=comment))

    def feedback_export(self) -> list[dict]:
        """Only consented feedback; private user IDs and full questions are intentionally excluded."""
        rows = self._all(select(feedback).order_by(feedback.c.created_at.desc()).limit(1000))
        return [{"run_id": r["run_id"], "rating": r["rating"], "category": r["category"],
                 "comment": r["comment"], "created_at": r["created_at"].isoformat()} for r in rows]

    def run_metrics(self) -> dict:
        rows = self._all(select(run_records.c.status, run_records.c.summary)
                         .where(run_records.c.status.not_in(["running", "paused"]))
                         .order_by(run_records.c.started_at.desc()).limit(200))
        stages: dict[str, list[float]] = {}
        for row in rows:
            for step, seconds in (row["summary"] or {}).get("stages", {}).items():
                stages.setdefault(step, []).append(float(seconds))
        return {"completed_runs": len(rows),
                "status_counts": {s: sum(r["status"] == s for r in rows)
                                  for s in sorted({r["status"] for r in rows})},
                "mean_stage_seconds": {k: round(sum(v) / len(v), 2) for k, v in stages.items()}}
