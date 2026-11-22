"""Store: quotas."""
from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    insert,
    select,
    update,
)

from ..config import get_settings
from .models import api_calls, usage


class QuotaStore:
    def reserve_api_call(self, pin: dict, run_id: str, agent: str, amount: float) -> str:
        from ..llm import BUDGET_KEY, BudgetExceeded
        cid, today = uuid.uuid4().hex, str(datetime.now(UTC).date())
        if amount < 0 or not math.isfinite(amount):
            raise ValueError('Invalid reservation')
        with self.engine.begin() as c:
            if pin['billing'] == 'shared':
                profile = self.ai_profile(pin['user_id'])
                limit = profile['daily_limit'] if profile.get('daily_limit') is not None else \
                    get_settings().daily_user_budget_usd
                for identity, budget in [(BUDGET_KEY, get_settings().daily_budget_usd), (pin['user_id'], limit)]:
                    c.execute(self._upsert(usage).values(user_id=identity, day=today, usd=0, questions=0)
                              .on_conflict_do_nothing())
                    changed = c.execute(update(usage).where(usage.c.user_id == identity, usage.c.day == today,
                        usage.c.usd + amount <= budget).values(usd=usage.c.usd + amount)).rowcount
                    if not changed:
                        raise BudgetExceeded('The shared daily spending allowance cannot cover this call. '
                                             'Connect a personal API key or try again tomorrow.')
            c.execute(insert(api_calls).values(id=cid, user_id=pin['user_id'], run_id=run_id, day=today,
                provider=pin['provider'], model=pin['model'], credential_id=pin.get('credential_id'),
                billing=pin['billing'], agent=agent, status='reserved', reserved_usd=amount))
        return cid

    def settle_api_call(self, cid: str, usd: float | None, tokens: dict, status: str = 'succeeded') -> None:
        from ..llm import BUDGET_KEY
        with self.engine.begin() as c:
            row = c.execute(update(api_calls).where(api_calls.c.id == cid, api_calls.c.status == 'reserved')
                .values(status=status, usd=usd, input_tokens=tokens.get('input', 0),
                        cached_tokens=tokens.get('cached', 0), output_tokens=tokens.get('output', 0))
                .returning(api_calls)).mappings().first()
            if not row:
                return
            if row['billing'] == 'shared':
                # Unknown outcomes keep their reservation charged. Never free a possibly billed call on a timeout.
                charged = row['reserved_usd'] if usd is None else usd
                for identity in (BUDGET_KEY, row['user_id']):
                    c.execute(update(usage).where(usage.c.user_id == identity, usage.c.day == row['day'])
                              .values(usd=usage.c.usd + charged - row['reserved_usd']))

    def api_usage(self, user_id: str | None = None) -> dict:
        today = str(datetime.now(UTC).date())
        stmt = select(api_calls).where(api_calls.c.day == today)
        if user_id is not None:
            stmt = stmt.where(api_calls.c.user_id == user_id)
        rows = self._all(stmt)
        groups = {}
        for row in rows:
            key = (row['user_id'], row['billing'], row['provider'], row['model'])
            group = groups.setdefault(key, dict(user_id=key[0], billing=key[1], provider=key[2], model=key[3],
                calls=0, input_tokens=0, cached_tokens=0, output_tokens=0, usd=0, uncertain_calls=0, reserved_usd=0))
            group['calls'] += 1
            for field in ('input_tokens', 'cached_tokens', 'output_tokens'):
                group[field] += row[field] or 0
            group['usd'] += row['usd'] or 0
            if row['usd'] is None:
                group['uncertain_calls'] += 1
                group['reserved_usd'] += row['reserved_usd'] or 0
        result = {"day": today, "timezone": "UTC", "groups": list(groups.values())}
        if user_id is not None:
            profile = self.ai_profile(user_id)
            limit = (profile['daily_limit'] if profile.get('daily_limit') is not None
                     else get_settings().daily_user_budget_usd)
            used = self.usd_today(user_id)
            result.update(requests=self.questions_today(user_id), shared_used=used,
                          daily_limit=limit, remaining=max(0, limit-used))
        return result

    def api_call_history(self, run_id: str, user_id: str) -> list[dict]:
        return self._all(select(api_calls).where(api_calls.c.run_id == run_id, api_calls.c.user_id == user_id)
                         .order_by(api_calls.c.created_at))

    def questions_today(self, user_id: str) -> int:
        row = self._one(select(usage).where(usage.c.user_id == user_id, usage.c.day == str(datetime.now(UTC).date())))
        return row["questions"] if row else 0

    def count_question(self, user_id: str, usd: float = 0.0) -> None:
        self.reserve_question(user_id, usd=usd)

    def reserve_question(self, user_id: str, limit: int | None = None, usd: float = 0.0) -> bool:
        """Check the daily quota and increment it atomically, including concurrent first requests."""
        if limit is not None and limit < 1:
            return False
        statement = self._upsert(usage).values(user_id=user_id, day=str(datetime.now(UTC).date()), questions=1, usd=usd)
        statement = statement.on_conflict_do_update(index_elements=[usage.c.user_id, usage.c.day],
            set_={"questions": usage.c.questions + 1, "usd": usage.c.usd + usd},
            where=usage.c.questions < limit if limit is not None else None)
        with self.engine.begin() as c:
            return c.execute(statement.returning(usage.c.user_id)).first() is not None

    def usd_today(self, key: str) -> float:
        row = self._one(select(usage).where(usage.c.user_id == key, usage.c.day == str(datetime.now(UTC).date())))
        return float(row["usd"]) if row else 0.0

    def add_usd_today(self, key: str, usd: float) -> None:
        """Add spend for today under a key (atomic update; the row is created on first use)."""
        with self.engine.begin() as c:
            c.execute(self._upsert(usage).values(user_id=key, day=str(datetime.now(UTC).date()),
                                               questions=0, usd=usd)
                .on_conflict_do_update(index_elements=[usage.c.user_id, usage.c.day], set_={"usd": usage.c.usd + usd}))
