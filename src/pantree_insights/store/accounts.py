"""Store: accounts."""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import timedelta

from sqlalchemy import (
    delete,
    insert,
    select,
    update,
)

from ..config import get_settings
from .models import _id, _now, ai_credentials, ai_profiles, email_codes, users


class AccountStore:
    def upsert_user(self, email: str, name: str | None = None, picture: str | None = None,
                    provider: str = "email") -> dict:
        email = email.strip().lower()
        is_admin = email in {e.strip().lower() for e in get_settings().admin_emails}
        existing = self._one(select(users).where(users.c.email == email))
        if existing:
            self._exec(update(users).where(users.c.id == existing["id"]).values(
                last_login_at=_now(), is_admin=is_admin, name=name or existing["name"],
                picture=picture or existing["picture"]))
        else:
            self._exec(insert(users).values(id=_id(), email=email, name=name or email.split("@")[0],
                                            picture=picture, auth_provider=provider, is_admin=is_admin))
        return self._one(select(users).where(users.c.email == email))

    def user(self, user_id: str) -> dict | None:
        return self._one(select(users).where(users.c.id == user_id))

    def set_preferences(self, user_id: str, text: str) -> None:
        self._exec(update(users).where(users.c.id == user_id).values(preferences=text[:4000]))

    def ai_profile(self, user_id: str) -> dict:
        row = self._one(select(ai_profiles).where(ai_profiles.c.user_id == user_id)) or {}
        return {"mode": "shared", "provider": "openai", "model": get_settings().model_main.split(":", 1)[-1],
                "credential_id": None, "daily_limit": None, **{k: v for k, v in row.items() if v is not None}}

    def set_ai_profile(self, user_id: str, **values) -> None:
        fields = ('mode', 'provider', 'model', 'credential_id', 'daily_limit')
        allowed = {k: v for k, v in values.items() if k in fields}
        self._exec(self._upsert(ai_profiles).values(user_id=user_id, **allowed).on_conflict_do_update(
            index_elements=[ai_profiles.c.user_id], set_=allowed))

    def add_credential(self, user_id: str, provider: str, key: str, model: str) -> str:
        from ..providers import encrypt_key
        cid = uuid.uuid4().hex
        encrypted = encrypt_key(user_id, cid, provider, key)
        with self.engine.begin() as c:
            # Serialize replacement/removal per owner, including concurrently inserted credential versions.
            c.execute(select(users.c.id).where(users.c.id == user_id).with_for_update())
            # Replacement revokes the old version. Pending runs fail explicitly, never use the replacement key.
            c.execute(update(ai_credentials).where(ai_credentials.c.user_id == user_id)
                      .values(active=False, ciphertext=""))
            c.execute(insert(ai_credentials).values(id=cid, user_id=user_id, provider=provider,
                      ciphertext=encrypted, suffix=key[-4:], active=True))
            values = dict(mode="personal", provider=provider, model=model, credential_id=cid)
            c.execute(self._upsert(ai_profiles).values(user_id=user_id, **values).on_conflict_do_update(
                index_elements=[ai_profiles.c.user_id], set_=values))
        return cid

    def credential(self, user_id: str, cid: str | None) -> dict | None:
        return self._one(select(ai_credentials).where(ai_credentials.c.id == cid,
                          ai_credentials.c.user_id == user_id, ai_credentials.c.active.is_(True))) if cid else None

    def remove_credentials(self, user_id: str) -> None:
        with self.engine.begin() as c:
            c.execute(select(users.c.id).where(users.c.id == user_id).with_for_update())
            c.execute(update(ai_credentials).where(ai_credentials.c.user_id == user_id)
                      .values(active=False, ciphertext="", suffix=""))
            c.execute(update(ai_profiles).where(ai_profiles.c.user_id == user_id).values(
                mode="shared", provider="openai", model=get_settings().model_main.split(":", 1)[-1],
                credential_id=None))

    def pin_ai(self, user: dict) -> dict:
        import os

        from ..providers import key_version
        profile = self.ai_profile(user['id'])
        return {"user_id": user['id'], "mode": profile['mode'], "provider": profile['provider'],
                "model": profile['model'],
                "credential_id": profile.get('credential_id') if profile['mode'] == 'personal' else None,
                "shared_version": key_version(os.getenv('OPENAI_API_KEY', '')) if profile['mode'] == 'shared' else None,
                "billing": "personal" if profile['mode'] == 'personal' else "admin" if user['is_admin'] else "shared"}

    def ai_key(self, pin: dict) -> str:
        from ..providers import ProviderError, decrypt_key, key_version, shared_key
        if pin['mode'] == 'personal':
            credential = self.credential(pin['user_id'], pin['credential_id'])
            if not credential or credential['provider'] != pin['provider']:
                raise ProviderError(
                    'This run’s credential was revoked. Start a new request with your current settings.', 409)
            return decrypt_key(pin['user_id'], credential)
        key = shared_key()
        if key_version(key) != pin['shared_version']:
            raise ProviderError(
                'Pantree credentials changed. Start a new request; this run will not switch credentials.', 409)
        return key

    def new_email_code(self, email: str, minutes: int = 10) -> str | None:
        """A 6-digit code, or None if one was sent less than 60 seconds ago (rate limit)."""
        email = email.strip().lower()
        code = f"{secrets.randbelow(10**6):06d}"
        values = {"code_hash": hashlib.sha256(f"{email}:{code}".encode()).hexdigest(),
                  "expires_at": _now() + timedelta(minutes=minutes), "attempts": 0, "sent_at": _now()}
        with self.engine.begin() as c:
            inserted = c.execute(self._upsert(email_codes).values(email=email, **values).on_conflict_do_update(
                index_elements=[email_codes.c.email], set_=values,
                where=email_codes.c.sent_at <= _now() - timedelta(seconds=60)).returning(email_codes.c.email)).first()
        return code if inserted else None

    def check_email_code(self, email: str, code: str) -> bool:
        email = email.strip().lower()
        with self.engine.begin() as c:
            row = c.execute(update(email_codes).where(email_codes.c.email == email,
                email_codes.c.expires_at >= _now(), email_codes.c.attempts < 5)
                .values(attempts=email_codes.c.attempts + 1).returning(email_codes.c.code_hash)).first()
            digest = hashlib.sha256(f"{email}:{code.strip()}".encode()).hexdigest()
            if not row or not secrets.compare_digest(row[0], digest):
                return False
            return c.execute(delete(email_codes).where(email_codes.c.email == email,
                                                       email_codes.c.code_hash == row[0])).rowcount == 1
