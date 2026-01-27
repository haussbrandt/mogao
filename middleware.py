import base64
import os
import secrets
import bcrypt
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        auth_header = request.headers.get("Authorization")

        if not auth_header or not auth_header.startswith("Basic "):
            return Response(
                content="Authentication required",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Secure Area"'},
            )

        try:
            credentials = base64.b64decode(auth_header[6:]).decode("utf-8")
            username, password = credentials.split(":", 1)

            username_correct = secrets.compare_digest(username, "admin")
            password_correct = bcrypt.checkpw(
                password.encode(), os.environ.get("MOGAO_ADMIN_HASH").encode()
            )

            if not (username_correct and password_correct):
                return Response(
                    content="Invalid credentials",
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="Secure Area"'},
                )
        except Exception:
            return Response(
                content="Invalid authentication",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Secure Area"'},
            )

        response = await call_next(request)
        return response
