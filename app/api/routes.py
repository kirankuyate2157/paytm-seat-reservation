import asyncio
import re
import uuid

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, StrictInt, field_validator
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.api.deps import CurrentUser, require_admin, require_user
from app.core import metrics
from app.core.config import settings
from app.core.errors import ApiError
from app.core.logging import correlation_id_var
from app.core.security import create_access_token, hash_password, verify_password
from app.db.models import User
from app.db.session import SessionLocal, db_gate, engine
from app.services import reservations as reservation_service
from app.services import shows as show_service

router = APIRouter()
SEAT_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_DUMMY_HASH: str | None = None


def ok(data: dict, status: int = 200, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({**data, "correlation_id": correlation_id_var.get()}, status_code=status, headers=headers)


def _parse_uuid(value: str, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise ApiError(400, "VALIDATION_ERROR", f"Invalid {what}", {what: "must be a UUID"})


def _clean_seats(seats: list[str]) -> list[str]:
    cleaned = [s.strip() for s in seats]
    bad = [s for s in cleaned if not SEAT_RE.match(s)]
    if bad:
        raise ValueError(f"invalid seat code(s): {bad[:3]}")
    return cleaned


# ---------- schemas ----------
class CredentialsBody(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=128)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip().lower()
        if "@" not in v:
            raise ValueError("must be a valid email")
        return v


class CreateShowBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    seats: list[str] = Field(min_length=1)
    price_paise: StrictInt = Field(ge=0)  # integer paise only; floats rejected
    per_user_limit: StrictInt | None = Field(default=None, ge=1, le=100)

    @field_validator("seats")
    @classmethod
    def _seats(cls, v: list[str]) -> list[str]:
        v = _clean_seats(v)
        if len(v) > settings.max_seats_per_show:
            raise ValueError(f"max {settings.max_seats_per_show} seats")
        if len(set(v)) != len(v):
            raise ValueError("duplicate seat codes")
        return v


class ReserveBody(BaseModel):
    # NOTE: unknown fields (e.g. a spoofed user_id) are ignored; identity is token-derived.
    seats: list[str] = Field(min_length=1)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("seats")
    @classmethod
    def _seats(cls, v: list[str]) -> list[str]:
        return _clean_seats(v)


# ---------- auth ----------
async def _issue(user_id: uuid.UUID, role: str, status: int) -> JSONResponse:
    token = create_access_token(user_id, role)
    resp = ok({"access_token": token, "token_type": "bearer", "user_id": str(user_id), "role": role,
               "expires_in": settings.access_token_ttl_seconds}, status)
    resp.set_cookie("access_token", token, httponly=True, secure=settings.cookie_secure, samesite="lax",
                    max_age=settings.access_token_ttl_seconds, path="/")
    return resp


@router.post("/auth/signup")
async def signup(body: CredentialsBody):
    pw = await hash_password(body.password)
    try:
        async with db_gate, SessionLocal() as s:
            user = User(email=body.email, password_hash=pw, role="user")
            s.add(user)
            await s.commit()
            uid, role = user.id, user.role
    except IntegrityError:
        raise ApiError(409, "EMAIL_EXISTS", "An account with this email already exists")
    return await _issue(uid, role, 201)


@router.post("/auth/login")
async def login(body: CredentialsBody):
    async with db_gate, SessionLocal() as s:
        row = (await s.execute(text("SELECT id, password_hash, role FROM users WHERE email = :e"), {"e": body.email})).first()
    # verify against a dummy hash when user is missing so response time does not reveal account existence
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = await hash_password("dummy-password-for-timing")
    stored = row[1] if row else _DUMMY_HASH
    valid = await verify_password(body.password, stored)
    if not row or not valid:
        metrics.AUTH_FAIL.labels("bad_credentials").inc()
        raise ApiError(401, "INVALID_CREDENTIALS", "Invalid email or password")
    return await _issue(row[0], row[2], 200)


@router.post("/auth/logout")
async def logout():
    resp = ok({"message": "Logged out"})
    resp.delete_cookie("access_token", path="/")
    return resp


# ---------- shows ----------
@router.post("/shows")
async def create_show(body: CreateShowBody, admin: CurrentUser = Depends(require_admin)):
    show = await show_service.create_show(
        body.name, body.seats, body.price_paise, body.per_user_limit or settings.default_per_user_limit
    )
    return ok(show, 201)


@router.get("/shows/{show_id}")
async def get_show(show_id: str, include_seats: bool = Query(True)):
    return ok(await show_service.show_state(_parse_uuid(show_id, "show_id"), include_seats))


@router.post("/shows/{show_id}/reserve")
async def reserve(
    show_id: str,
    body: ReserveBody,
    user: CurrentUser = Depends(require_user),
    idempotency_key_header: str | None = Header(default=None, alias="Idempotency-Key"),
):
    key = idempotency_key_header or body.idempotency_key
    if not key:
        raise ApiError(400, "VALIDATION_ERROR", "idempotency key required", {"idempotency_key": "header Idempotency-Key or body field required"})
    if len(key) > 200:
        raise ApiError(400, "VALIDATION_ERROR", "idempotency key too long", {"idempotency_key": "max 200 chars"})
    payload, replay = await reservation_service.reserve(user.id, _parse_uuid(show_id, "show_id"), body.seats, key)
    return ok(payload, 201, headers={"Idempotent-Replay": "true"} if replay else None)


@router.post("/reservations/{reservation_id}/cancel")
async def cancel_reservation(reservation_id: str, user: CurrentUser = Depends(require_user)):
    return ok(await reservation_service.cancel(user.id, _parse_uuid(reservation_id, "reservation_id")))


# ---------- health & metrics ----------
@router.get("/health/live")
async def live():
    return ok({"status": "alive"})


@router.get("/health/ready")
async def ready():
    try:
        async with asyncio.timeout(2):
            async with engine.connect() as c:
                await c.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse(
            {"status": "unavailable", "dependency": "database", "correlation_id": correlation_id_var.get()}, status_code=503
        )
    return ok({"status": "ready"})


@router.get("/metrics")
async def prom_metrics():
    # Seat gauges are computed from the DB at scrape time so they reconcile exactly with GET /shows/{id}.
    try:
        async with asyncio.timeout(5):
            async with engine.connect() as c:
                rows = (
                    await c.execute(
                        text(
                            "SELECT show_id, state, count(*) FROM seats WHERE show_id IN "
                            "(SELECT id FROM shows ORDER BY created_at DESC LIMIT 200) GROUP BY show_id, state"
                        )
                    )
                ).all()
        per_show: dict[str, dict[str, int]] = {}
        for show_id, state, n in rows:
            per_show.setdefault(str(show_id), {"available": 0, "held": 0, "confirmed": 0})[state] = n
        for sid, c in per_show.items():
            for state, n in c.items():
                metrics.SEATS.labels(sid, state).set(n)
            metrics.SEATS_AVAILABLE.labels(sid).set(c["available"])
            metrics.SEATS_HELD.labels(sid).set(c["held"])
            metrics.SEATS_CONFIRMED.labels(sid).set(c["confirmed"])
    except Exception:
        pass  # counters still served if DB is down; readiness reports the DB failure
    return Response(metrics.render(), media_type="text/plain; version=0.0.4; charset=utf-8")
