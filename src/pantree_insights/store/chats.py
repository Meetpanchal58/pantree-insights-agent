"""Store: chats."""
from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import (
    delete,
    insert,
    or_,
    select,
    update,
)

from ..memory import _tokens
from .models import _id, _now, chats, investigations, memories, messages, projects, run_records
from .serialization import to_json


class ChatStore:
    def projects(self, user_id: str) -> list[dict]:
        return self._all(select(projects).where(projects.c.user_id == user_id).order_by(projects.c.updated_at.desc()))

    def project(self, user_id: str, project_id: str) -> dict | None:
        return self._one(select(projects).where(projects.c.id == project_id, projects.c.user_id == user_id))

    def create_project(self, user_id: str, name: str, instructions: str = "") -> dict:
        pid = _id()
        self._exec(insert(projects).values(id=pid, user_id=user_id, name=name[:200], instructions=instructions[:4000]))
        return self.project(user_id, pid)

    def update_project(self, user_id: str, project_id: str, **values: Any) -> dict | None:
        values = {k: v for k, v in values.items() if k in ("name", "instructions") and v is not None}
        if values:
            self._exec(update(projects).where(projects.c.id == project_id, projects.c.user_id == user_id)
                       .values(**values, updated_at=_now()))
        return self.project(user_id, project_id)

    def delete_project(self, user_id: str, project_id: str) -> None:
        with self.engine.begin() as c:   # chats stay (moved out of the project); project memory goes
            c.execute(update(chats).where(chats.c.project_id == project_id, chats.c.user_id == user_id)
                      .values(project_id=None))
            c.execute(delete(memories).where(memories.c.project_id == project_id, memories.c.user_id == user_id))
            c.execute(delete(projects).where(projects.c.id == project_id, projects.c.user_id == user_id))

    def chats(self, user_id: str, project_id: str | None = None) -> list[dict]:
        stmt = select(chats).where(chats.c.user_id == user_id)
        if project_id:
            stmt = stmt.where(chats.c.project_id == project_id)
        return self._all(stmt.order_by(chats.c.updated_at.desc()).limit(200))

    def chat(self, user_id: str, chat_id: str) -> dict | None:
        return self._one(select(chats).where(chats.c.id == chat_id, chats.c.user_id == user_id))

    def create_chat(self, user_id: str, project_id: str | None = None, title: str = "New chat") -> dict:
        if project_id and not self.project(user_id, project_id):
            project_id = None
        cid = _id()
        self._exec(insert(chats).values(id=cid, user_id=user_id, project_id=project_id, title=title[:200]))
        return self.chat(user_id, cid)

    def update_chat(self, user_id: str, chat_id: str, title: str | None = None,
                    project_id: str | None | bool = False) -> dict | None:
        values: dict[str, Any] = {"updated_at": _now()}
        if title:
            values["title"] = title[:200]
        if project_id is not False:                       # move into / out of a project
            values["project_id"] = project_id if project_id and self.project(user_id, project_id) else None
        self._exec(update(chats).where(chats.c.id == chat_id, chats.c.user_id == user_id).values(**values))
        return self.chat(user_id, chat_id)

    def delete_chat(self, user_id: str, chat_id: str) -> None:
        if self.chat(user_id, chat_id):
            with self.engine.begin() as c:
                c.execute(delete(investigations).where(investigations.c.chat_id == chat_id))
                c.execute(delete(messages).where(messages.c.chat_id == chat_id))
                c.execute(delete(chats).where(chats.c.id == chat_id))

    def fork_message(self, user_id: str, chat_id: str, message_id: str, content: str,
                     expected: str, key: str) -> dict:
        """Create an edited copy with only preceding history; never rewrite downstream evidence."""
        from ..editing import reconstruction
        branch_id = hashlib.sha256(f"{user_id}:{chat_id}:{message_id}:{key}".encode()).hexdigest()[:16]
        edit_id = hashlib.sha256(f"{branch_id}:edited".encode()).hexdigest()[:16]
        with self.engine.begin() as c:
            source = c.execute(select(chats).where(chats.c.id == chat_id,
                                chats.c.user_id == user_id).with_for_update()).mappings().first()
            if not source:
                raise LookupError('Chat not found')
            existing = c.execute(select(messages).where(messages.c.id == edit_id)).mappings().first()
            if existing:
                info = existing['run'] or {}
                if existing['content'] != content or info.get('original') != expected:
                    raise ValueError('This edit key was already used for a different message.')
                return {'chat_id': branch_id, 'message_id': edit_id, **info['reconstruction']}
            rows = list(c.execute(select(messages).where(messages.c.chat_id == chat_id)
                        .order_by(messages.c.created_at, messages.c.id)).mappings())
            index = next((i for i, m in enumerate(rows) if m['id'] == message_id and m['role'] == 'user'), None)
            if index is None:
                raise LookupError('Your message was not found')
            if rows[index]['content'] != expected:
                raise ValueError('This message changed. Reopen the chat before editing it.')
            seed = reconstruction(rows[:index], content)
            if len(seed['question']) > 500:
                raise ValueError('An analytical question must contain at most 500 characters.')
            c.execute(insert(chats).values(id=branch_id, user_id=user_id, project_id=source['project_id'],
                                          title=('Edited: ' + seed['question'])[:200]))
            latest = None
            for row in rows[:index]:
                saved = dict(row['run'] or {})
                if saved.get('status') == 'done' and saved.get('type') == 'final':
                    origin = saved.get('run_id')
                    record = c.execute(select(run_records).where(run_records.c.id == origin,
                                       run_records.c.user_id == user_id)).mappings().first() if origin else None
                    if record:
                        copied_id = hashlib.sha256(f"{branch_id}:{origin}".encode()).hexdigest()[:32]
                        saved = {**saved, 'run_id': copied_id, 'source_run_id': origin}
                        c.execute(self._upsert(run_records).values(id=copied_id, user_id=user_id,
                            chat_id=branch_id, question=record['question'], status='done', events=to_json([saved]),
                            summary=to_json({'snapshot': (record['summary'] or {}).get('snapshot'),
                                             'source_run_id': origin}), started_at=record['started_at'])
                            .on_conflict_do_nothing())
                    if saved.get('grounded'):
                        latest = saved
                c.execute(insert(messages).values(id=hashlib.sha256(f"{branch_id}:{row['id']}".encode()).hexdigest()[:16],
                    chat_id=branch_id, role=row['role'], content=row['content'], run=to_json(saved) if saved else None,
                    created_at=row['created_at']))
            info = {'kind': 'edited', 'source_chat_id': chat_id, 'source_message_id': message_id,
                    'original': expected, 'reconstruction': seed}
            c.execute(insert(messages).values(id=edit_id, chat_id=branch_id, role='user', content=content, run=info))
            if latest:
                c.execute(insert(investigations).values(chat_id=branch_id, context=to_json({
                    'objective': latest.get('objective'), 'current_plan': latest.get('confirmed_plan') or [],
                    'populations': latest.get('populations') or [],
                    'metric_definitions': latest.get('metric_definitions') or {}, 'evidence': []})))
        return {'chat_id': branch_id, 'message_id': edit_id, **seed}

    def messages(self, chat_id: str) -> list[dict]:
        return self._all(select(messages).where(messages.c.chat_id == chat_id)
                         .order_by(messages.c.created_at, messages.c.id))

    def add_message(self, chat_id: str, role: str, content: str, run: dict | None = None) -> str:
        mid = _id()
        with self.engine.begin() as c:
            c.execute(insert(messages).values(id=mid, chat_id=chat_id, role=role, content=content, run=run))
            c.execute(update(chats).where(chats.c.id == chat_id).values(updated_at=_now()))
        return mid

    def history(self, chat_id: str, turns: int = 4) -> str:
        return self.conversation(chat_id, turns)[0]

    def investigation(self, user_id: str, chat_id: str) -> dict:
        row = self._one(select(investigations.c.context).join(chats, chats.c.id == investigations.c.chat_id)
                        .where(chats.c.user_id == user_id, chats.c.id == chat_id))
        return row["context"] if row else {}

    def recall(self, user_id: str, chat_id: str, question: str, snapshot: str | None) -> dict:
        """Search durable completed runs across this chat; fetch full payloads only for top matches.

        Historical findings are evidence, never standing instructions. No context-window cutoff is used.
        """
        import re
        words = list(dict.fromkeys(_tokens(question)))[:16]
        ignored = {"these", "those", "their", "earlier", "previous", "analysis", "compare", "show", "same",
                   "question", "investigate", "please", "first", "again", "results", "using"}
        words = [w for w in words if w not in ignored and len(w) >= 3]
        ids = re.findall(r"\b[a-f0-9]{12,32}\b", question)
        if not words and not ids:
            return {}
        matches = [run_records.c.question.ilike('%' + w.replace('%', '').replace('_', '') + '%') for w in words]
        if ids:
            matches.append(run_records.c.id.in_(ids))
        query = select(run_records.c.id, run_records.c.question, run_records.c.started_at).join(
            chats, chats.c.id == run_records.c.chat_id).where(
            chats.c.user_id == user_id, run_records.c.user_id == user_id, run_records.c.chat_id == chat_id,
            run_records.c.status == 'done', or_(*matches))
        # Rank in SQL so an old, strongly matching question is not lost behind recent weak matches.
        from sqlalchemy import case
        score = sum(case((run_records.c.question.ilike('%' + w + '%'), 1), else_=0) for w in words)
        score += case((run_records.c.id.in_(ids), 100), else_=0)
        rows = self._all(query.order_by(score.desc(), run_records.c.started_at.desc()).limit(3))
        evidence, historical = [], []
        for row in rows:
            record = self.run_record(user_id, row['id'])
            final = next((e for e in reversed(record.get('events') or []) if e.get('type') == 'final'), {})
            if not final.get('grounded') or not final.get('confirmed_plan'):
                continue
            old_snapshot = (record.get('summary') or {}).get('snapshot')
            stale = not snapshot or old_snapshot != snapshot
            evidence.append({'run_id': row['id'], 'question': row['question'],
                'created_at': str(row['started_at']), 'snapshot': old_snapshot, 'stale': stale,
                'plan': [{k: v for k, v in p.items() if k != 'sql'} for p in final['confirmed_plan']],
                'finding': (final.get('analysis') or {}).get('headline', ''),
                'hypotheses': final.get('probe_ledger', []),
                'instruction': 'Historical evidence only. Recompute stale numbers; '
                               'do not override current corrections.'})
            for population in final.get('populations') or []:
                historical.append({**population, 'run_id': row['id'],
                                   'complete': bool(population.get('complete')) and not stale})
        return {'retrieved_evidence': evidence, 'historical_populations': historical}

    def conversation(self, chat_id: str, turns: int = 4) -> tuple[str, dict | None]:
        """Fetch bounded history and the latest completed plan in one database roundtrip."""
        rows = list(reversed(self._all(select(messages).where(messages.c.chat_id == chat_id)
                    .order_by(messages.c.created_at.desc()).limit(max(1, min(turns, 20)) * 2))))
        previous = None
        lines = []
        for m in rows:
            if m["role"] == "user":
                lines.append(f"User: {m['content']}")
            else:
                run = m.get("run") or {}
                if run.get("superseded_by_edit"):
                    continue
                if run.get("status") == "done":
                    previous = run.get("spec")
                plan = run.get("restated") or run.get("readback")
                lines.append(f"Analyst: {m['content']}" + (f" (analysed: {plan})" if plan else ""))
        return "\n".join(lines), previous
