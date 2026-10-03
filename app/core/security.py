import uuid
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi.concurrency import run_in_threadpool

from app.core.config import settings

# OWASP minimum argon2id profile; hashing runs in a thread pool so it never blocks the event loop.
_ph = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)


async def hash_password(password: str) -> str:
    return await run_in_threadpool(_ph.hash, password)


async def verify_password(password: str, hashed: str) -> bool:
    def _verify() -> bool:
        try:
            return _ph.verify(hashed, password)
        except (VerificationError, InvalidHashError):
            return False

    return await run_in_threadpool(_verify)


def create_access_token(user_id: uuid.UUID, role: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "role": role,
        "iat": now,
        "exp": now + timedelta(seconds=settings.access_token_ttl_seconds),
        "jti": uuid.uuid4().hex,
        "iss": "seat-reservation",
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    return jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
        issuer="seat-reservation",
        options={"require": ["exp", "sub", "iss"]},
    )
