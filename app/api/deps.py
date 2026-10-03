import uuid
from dataclasses import dataclass

import jwt
from fastapi import Request

from app.core.errors import ApiError
from app.core.logging import user_id_var
from app.core.metrics import AUTH_FAIL
from app.core.security import decode_access_token


@dataclass(frozen=True)
class CurrentUser:
    id: uuid.UUID
    role: str


def _extract_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.cookies.get("access_token")


async def require_user(request: Request) -> CurrentUser:
    """Identity comes ONLY from the verified token — never from the request body."""
    token = _extract_token(request)
    if not token:
        AUTH_FAIL.labels("missing").inc()
        raise ApiError(401, "UNAUTHORIZED", "Authentication required")
    try:
        claims = decode_access_token(token)
        user = CurrentUser(id=uuid.UUID(claims["sub"]), role=claims.get("role", "user"))
    except (jwt.PyJWTError, ValueError):
        AUTH_FAIL.labels("invalid").inc()
        raise ApiError(401, "UNAUTHORIZED", "Invalid or expired token")
    user_id_var.set(str(user.id))
    return user


async def require_admin(request: Request) -> CurrentUser:
    user = await require_user(request)
    if user.role != "admin":
        AUTH_FAIL.labels("forbidden").inc()
        raise ApiError(403, "FORBIDDEN", "Admin role required")
    return user
