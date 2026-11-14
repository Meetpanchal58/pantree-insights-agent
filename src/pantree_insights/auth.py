"""Sign-in: Google (OAuth / OpenID Connect) and one-time email codes. Sessions are signed cookies.

Google needs GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET (an OAuth client in Google Cloud) with the redirect
URI <PUBLIC_URL>/auth/google/callback. Email codes need SMTP settings; in development without SMTP the
code is printed to the server log instead of being emailed.
"""
from __future__ import annotations

import logging
import re
import smtplib
from email.message import EmailMessage

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from .config import get_settings
from .store import Store, get_store

log = logging.getLogger("pantree.auth")
router = APIRouter()
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[a-z]{2,}$", re.I)
_oauth = None


def oauth():
    global _oauth
    settings = get_settings()
    if _oauth is None and settings.google_client_id:
        from authlib.integrations.starlette_client import OAuth

        _oauth = OAuth()
        _oauth.register("google", client_id=settings.google_client_id, client_secret=settings.google_client_secret,
                        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
                        client_kwargs={"scope": "openid email profile"})
    return _oauth


def methods() -> dict[str, bool]:
    s = get_settings()
    return {"google": bool(s.google_client_id and s.google_client_secret),
            "email": bool(s.smtp_host) or s.app_env == "development"}


# ----------------------------------------------------------------------------- current user
def current_user(request: Request, store: Store = Depends(get_store)) -> dict | None:
    user_id = request.session.get("user_id")
    return store.user(user_id) if user_id else None


def require_user(user: dict | None = Depends(current_user)) -> dict:
    if not user:
        raise HTTPException(401, "Please sign in.")
    return user


def require_admin(user: dict = Depends(require_user)) -> dict:
    if not user["is_admin"]:
        raise HTTPException(403, "Admins only.")
    return user


def _sign_in(request: Request, user: dict) -> None:
    request.session.clear()
    request.session["user_id"] = user["id"]


# ----------------------------------------------------------------------------- Google
@router.get("/auth/google/login")
async def google_login(request: Request):
    client = oauth()
    if not client:
        raise HTTPException(404, "Google sign-in is not configured.")
    return await client.google.authorize_redirect(request, f"{get_settings().public_url}/auth/google/callback")


@router.get("/auth/google/callback")
async def google_callback(request: Request, store: Store = Depends(get_store)):
    client = oauth()
    if not client:
        raise HTTPException(404, "Google sign-in is not configured.")
    try:
        token = await client.google.authorize_access_token(request)
    except Exception as error:                                  # cancelled, expired state, ...
        log.warning("Google sign-in failed: %s", error)
        return RedirectResponse("/?signin=failed")
    info = token.get("userinfo") or {}
    if not info.get("email") or not info.get("email_verified", False):
        return RedirectResponse("/?signin=failed")
    user = store.upsert_user(info["email"], info.get("name"), info.get("picture"), provider="google")
    _sign_in(request, user)
    return RedirectResponse("/")


# ----------------------------------------------------------------------------- email codes
class EmailRequest(BaseModel):
    email: str


class EmailVerify(BaseModel):
    email: str
    code: str


def send_code(email: str, code: str) -> None:
    s = get_settings()
    if not s.smtp_host:
        if s.app_env == "development":
            log.warning("DEV sign-in code for %s: %s", email, code)
            print(f"\n*** Sign-in code for {email}: {code} ***\n", flush=True)
            return
        raise HTTPException(503, "Email sign-in is not configured.")
    msg = EmailMessage()
    msg["Subject"] = f"{code} is your Pantree Insights sign-in code"
    msg["From"] = f"{s.smtp_from_name} <{s.smtp_from_email or s.smtp_username}>"
    msg["To"] = email
    msg.set_content(f"Your sign-in code is {code}. It expires in 10 minutes.\n\n"
                    "If you did not ask for it, you can ignore this email.")
    with smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=15) as smtp:
        smtp.login(s.smtp_username, s.smtp_password)
        smtp.send_message(msg)


@router.post("/auth/email/request")
def email_request(body: EmailRequest, store: Store = Depends(get_store)):
    email = body.email.strip().lower()
    if not EMAIL.match(email):
        raise HTTPException(400, "Enter a valid email address.")
    if not methods()["email"]:
        raise HTTPException(503, "Email sign-in is not configured.")
    code = store.new_email_code(email)
    if code is None:
        raise HTTPException(429, "A code was sent less than a minute ago. Please wait.")
    send_code(email, code)
    return {"sent": True}


@router.post("/auth/email/verify")
def email_verify(body: EmailVerify, request: Request, store: Store = Depends(get_store)):
    email = body.email.strip().lower()
    if not store.check_email_code(email, body.code):
        raise HTTPException(400, "That code is wrong or has expired.")
    user = store.upsert_user(email, provider="email")
    _sign_in(request, user)
    return {"ok": True}


@router.post("/auth/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}
