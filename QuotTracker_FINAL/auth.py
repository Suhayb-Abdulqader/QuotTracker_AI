"""Application authentication and request protections."""

import hmac
import os
import secrets
from urllib.parse import urlsplit

from fastapi import HTTPException, Request


ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
APP_ENV = os.getenv("APP_ENV", "development").lower()
COOKIE_SECURE = os.getenv(
    "COOKIE_SECURE", "true" if APP_ENV == "production" else "false"
).lower() == "true"
SESSION_MAX_AGE = 60 * 60 * 8

if len(SESSION_SECRET) < 32:
    raise RuntimeError("SESSION_SECRET must be set to at least 32 characters.")
if SESSION_SECRET.lower().startswith("replace-with-"):
    raise RuntimeError("SESSION_SECRET must not use the example placeholder.")
if not ADMIN_USERNAME or len(ADMIN_USERNAME) > 128:
    raise RuntimeError("ADMIN_USERNAME must be set and no longer than 128 characters.")
if len(ADMIN_PASSWORD) < 14:
    raise RuntimeError("ADMIN_PASSWORD must be set to at least 14 characters.")
if ADMIN_PASSWORD.lower().startswith("replace-with-"):
    raise RuntimeError("ADMIN_PASSWORD must not use the example placeholder.")
if APP_ENV == "production" and not COOKIE_SECURE:
    raise RuntimeError("COOKIE_SECURE=true is required when APP_ENV=production.")

def credentials_match(username: str, password: str) -> bool:
    username_matches = hmac.compare_digest(
        username.encode("utf-8"), ADMIN_USERNAME.encode("utf-8")
    )
    password_matches = hmac.compare_digest(
        password.encode("utf-8"), ADMIN_PASSWORD.encode("utf-8")
    )
    return username_matches and password_matches


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def require_authenticated(request: Request) -> None:
    if not request.session.get("authenticated"):
        raise HTTPException(status_code=401, detail="Authentication required")


def require_csrf(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin:
        parsed = urlsplit(origin)
        host = request.headers.get("host", "").lower()
        if not parsed.netloc or parsed.netloc.lower() != host:
            raise HTTPException(status_code=403, detail="Cross-origin request rejected")

    supplied = request.headers.get("x-csrf-token", "")
    expected = request.session.get("csrf_token", "")
    if not supplied or not expected or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
