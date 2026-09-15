import base64
import hashlib
import hmac
import logging
import os
import secrets
import time

import bcrypt
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

for name in ("MOGAO_ADMIN_USERNAME", "MOGAO_ADMIN_HASH", "MOGAO_SESSION_SECRET"):
    if not os.environ.get(name, "").strip():
        raise RuntimeError(f"{name} must be set in .env. Run ./quickstart.sh.")

COOKIE_NAME = "session"
SESSION_SECRET = os.environ[
    "MOGAO_SESSION_SECRET"
]  # generate with: secrets.token_hex(32)
if len(SESSION_SECRET.strip()) < 32:
    raise RuntimeError("MOGAO_SESSION_SECRET must contain at least 32 characters.")
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days


def _sign(value: str) -> str:
    sig = hmac.new(SESSION_SECRET.encode(), value.encode(), hashlib.sha256).hexdigest()
    return f"{value}.{sig}"


def _verify(signed: str) -> str | None:
    """Returns the payload if valid, None otherwise."""
    try:
        value, sig = signed.rsplit(".", 1)
        expected = hmac.new(
            SESSION_SECRET.encode(), value.encode(), hashlib.sha256
        ).hexdigest()
        if not secrets.compare_digest(sig, expected):
            return None
        return value
    except Exception:
        return None


def _make_token() -> str:
    payload = f"{secrets.token_hex(32)}.{int(time.time()) + SESSION_MAX_AGE}"
    return _sign(payload)


def _token_valid(signed: str) -> bool:
    payload = _verify(signed)
    if not payload:
        return False
    _, expires_at = payload.rsplit(".", 1)
    return int(time.time()) < int(expires_at)


PUBLIC_PATHS = {"/static/site.webmanifest"}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)

        # 1. Valid session cookie → just proceed
        cookie = request.cookies.get(COOKIE_NAME)
        if cookie and _token_valid(cookie):
            return await call_next(request)

        # 2. Basic Auth credentials supplied → validate and mint cookie
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Basic "):
            try:
                credentials = base64.b64decode(auth_header[6:]).decode()
                username, password = credentials.split(":", 1)
                username_ok = secrets.compare_digest(
                    username, os.environ["MOGAO_ADMIN_USERNAME"]
                )
                password_ok = bcrypt.checkpw(
                    password.encode(),
                    os.environ["MOGAO_ADMIN_HASH"].encode(),
                )
                if username_ok and password_ok:
                    response = await call_next(request)
                    response.set_cookie(
                        key=COOKIE_NAME,
                        value=_make_token(),
                        max_age=SESSION_MAX_AGE,
                        httponly=True,
                        secure=True,  # drop to False if testing over plain HTTP
                        samesite="strict",
                    )
                    return response
            except Exception:
                logger.exception("Error while validating Basic Auth credentials")

        # 3. Nothing valid → challenge
        return Response(
            content="Authentication required",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Secure Area"'},
        )
