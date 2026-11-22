"""Register routes account endpoints with the app-owned web.runner."""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import Depends, HTTPException
from fastapi.responses import JSONResponse

from .. import providers
from ..auth import current_user, methods, require_admin, require_user
from ..db import manifest
from ..store import Store, get_store
from .models import (
    AISettingsIn,
    AllowanceIn,
    ConnectionIn,
)

log = logging.getLogger("pantree.web")

def register(app, settings):

    @app.get("/api/me")
    def me(user: dict | None = Depends(current_user), store: Store = Depends(get_store)):
        info = manifest()
        base = {"signin": methods(), "data": {"size": info["size"], "first_day": info["first_day"],
                                              "last_day": info["last_day"]}}
        if not user:
            return {**base, "user": None}
        used = store.questions_today(user["id"])
        return {**base, "user": {k: user[k] for k in ("id", "email", "name", "picture", "is_admin")},
                "limit": {"per_day": settings.daily_question_limit, "used": used,
                          "unlimited": bool(user["is_admin"])}, "ai": public_ai(store, user)}

    def public_ai(store, user):
        profile = store.ai_profile(user['id'])
        credential = store.credential(user['id'], profile.get('credential_id'))
        import os
        return {**profile, 'credential_id': None, 'user_id': None,
            'default_provider': 'openai', 'default_model': settings.model_main.split(':', 1)[-1],
            'light_model': settings.model_light, 'encryption_ready': providers.encryption_ready(),
            'shared_configured': bool(os.getenv('OPENAI_API_KEY')),
            'personal': {'provider': credential['provider'], 'suffix': credential['suffix']} if credential else None,
            'usage': store.api_usage(user['id']), 'exempt': bool(user['is_admin']) or profile['mode'] == 'personal'}

    @app.get('/api/ai/settings')
    def ai_settings(user=Depends(require_user), store: Store = Depends(get_store)):
        return JSONResponse(public_ai(store, user), headers={'Cache-Control': 'no-store'})

    def account_key(store, user, mode, provider):
        if mode == 'shared':
            if provider != 'openai':
                raise HTTPException(400, 'Pantree default credentials use OpenAI')
            return providers.shared_key()
        profile = store.ai_profile(user['id'])
        credential = store.credential(user['id'], profile.get('credential_id'))
        if not credential or credential['provider'] != provider:
            raise HTTPException(409, 'Connect a key for this provider first')
        return providers.decrypt_key(user['id'], credential)

    @app.get('/api/ai/models')
    def ai_models(mode: Literal['shared', 'personal'] = 'shared', provider: providers.Provider = 'openai',
                  user=Depends(require_user), store: Store = Depends(get_store)):
        key = account_key(store, user, mode, provider)
        return JSONResponse({'models': providers.discover(provider, key, user['id'])},
                            headers={'Cache-Control': 'no-store'})

    @app.post('/api/ai/test')
    def ai_test(body: ConnectionIn, user=Depends(require_user)):
        if not providers.encryption_ready():
            raise HTTPException(503, 'The server must configure credential encryption before connecting personal keys')
        models = providers.discover(body.provider, body.api_key.get_secret_value(), user['id'], refresh=True)
        return {'connected': True, 'models': models,
                'message': 'Credentials accepted by the model listing API. No billable inference was performed.'}

    @app.put('/api/ai/settings')
    def save_ai_settings(body: AISettingsIn, user=Depends(require_user), store: Store = Depends(get_store)):
        if body.mode == 'shared' and (body.provider != 'openai' or body.api_key):
            raise HTTPException(400, 'Personal keys cannot be saved as Pantree default credentials')
        key = body.api_key.get_secret_value() if body.api_key else account_key(store, user, body.mode, body.provider)
        models = providers.discover(body.provider, key, user['id'])
        if body.model not in {m['id'] for m in models}:
            raise HTTPException(400, 'Select a compatible model returned by your active provider account')
        if body.api_key:
            store.add_credential(user['id'], body.provider, key, body.model)
        else:
            store.set_ai_profile(user['id'], mode=body.mode, provider=body.provider, model=body.model)
        return public_ai(store, user)

    @app.delete('/api/ai/credentials')
    def remove_ai_credentials(user=Depends(require_user), store: Store = Depends(get_store)):
        store.remove_credentials(user['id'])
        return public_ai(store, user)

    @app.get('/api/admin/usage')
    def admin_usage(user=Depends(require_admin), store: Store = Depends(get_store)):
        from sqlalchemy import select

        from ..store import users
        return {**store.api_usage(), 'shared_budget': settings.daily_budget_usd,
                'users': [{'id': u['id'], 'name': u['name'], 'is_admin': u['is_admin'],
                           'usage': store.api_usage(u['id'])} for u in store._all(select(users).limit(500))]}

    @app.put('/api/admin/usage/{user_id}')
    def set_allowance(user_id: str, body: AllowanceIn, user=Depends(require_admin), store: Store = Depends(get_store)):
        if not store.user(user_id):
            raise HTTPException(404, 'User not found')
        store.set_ai_profile(user_id, daily_limit=body.daily_limit)
        return store.api_usage(user_id)
