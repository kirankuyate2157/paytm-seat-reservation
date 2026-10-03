"""Reserve / cancel — the atomic decision lives here.

Lock order (identical in reserve and cancel, so no deadlock cycle is possible):
    reservation row  ->  user_show_quota row  ->  seat rows in sorted seat_code order
Reserve decision = blocking SELECT .. FOR UPDATE on the requested seats (sorted) followed by a guarded
UPDATE (.. WHERE state='available'). Backstop = partial unique index on reservation_seats(seat_id).
All-or-nothing: any problem raises Decline inside the transaction => whole transaction (incl. quota and the
idempotency claim) rolls back.
"""
import hashlib
import json
import logging
import time
import uuid

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core import metrics
from app.core.config import settings
from app.core.errors import ApiError, Decline
from app.core.logging import log
from app.db.session import SessionLocal, db_gate
from app.services.shows import get_show_meta

logger = logging.getLogger("app.reserve")
RETRYABLE_SQLSTATES = {"40P01", "40001"}  # deadlock_detected, serialization_failure


class RetryTransaction(Exception):
    """Internal: transaction should be re-run (e.g. idempotency claimant rolled back mid-flight)."""


def _sqlstate(exc: DBAPIError) -> str | None:
    orig = getattr(exc, "orig", None)
    return getattr(orig, "sqlstate", None) or getattr(getattr(orig, "__cause__", None), "sqlstate", None)


def _payload(rid, show_id, user_id, codes, amount, status):
    return {
        "reservation_id": str(rid),
        "show_id": str(show_id),
        "user_id": str(user_id),
        "seats": codes,
        "amount_paise": int(amount),
        "status": status,
    }


async def reserve(user_id: uuid.UUID, show_id: uuid.UUID, seats: list[str], idem_key: str) -> tuple[dict, bool]:
    """Returns (payload, is_replay). Raises Decline (4xx) for domain outcomes."""
    t0 = time.perf_counter()
    meta = await get_show_meta(show_id)
    codes = sorted(set(seats))
    if len(codes) > settings.max_seats_per_request:
        raise ApiError(400, "VALIDATION_ERROR", "Too many seats in one request", {"seats": f"max {settings.max_seats_per_request}"})
    req_hash = hashlib.sha256(json.dumps(codes).encode()).hexdigest()
    outcome = "error"
    try:
        pre = await _precheck(user_id, show_id, meta, codes, req_hash, idem_key)
        if pre is not None:
            result = pre
            outcome = "replay"
        for attempt in range(4 if pre is None else 0):
            try:
                async with db_gate, SessionLocal() as s:
                    async with s.begin():
                        result = await _reserve_tx(s, user_id, show_id, meta, codes, req_hash, idem_key)
                outcome = "replay" if result[1] else "confirmed"
                break
            except RetryTransaction:
                if attempt < 3:
                    continue
                raise ApiError(503, "SERVICE_UNAVAILABLE", "Please retry")
            except DBAPIError as e:
                if _sqlstate(e) in RETRYABLE_SQLSTATES and attempt < 3:
                    log(logger, "retrying transaction", level=logging.WARNING, sqlstate=_sqlstate(e), attempt=attempt)
                    continue
                raise
        payload, replay = result
        if replay:
            metrics.REPLAYS.inc()
            metrics.DECLINED.labels("idempotent-replay").inc()
        else:
            metrics.CONFIRMED.inc()
        log(logger, "reservation_attempt", action="reserve", show_id=str(show_id), seats=codes,
            outcome=outcome, latency_ms=round((time.perf_counter() - t0) * 1000, 1))
        return payload, replay
    except Decline as d:
        outcome = d.reason or "declined"
        metrics.DECLINED.labels(d.reason).inc()
        log(logger, "reservation_attempt", action="reserve", show_id=str(show_id), seats=codes,
            outcome="declined", reason=d.reason, latency_ms=round((time.perf_counter() - t0) * 1000, 1))
        raise
    finally:
        metrics.LATENCY.labels(outcome).observe(time.perf_counter() - t0)


async def _existing_result(s, user_id, show_id, idem_key, req_hash, codes):
    """Return (payload, True) if this idempotency key already has a reservation; raise on body mismatch."""
    existing = (
        await s.execute(
            text(
                "SELECT id, request_hash, status, amount_paise FROM reservations "
                "WHERE user_id = :u AND show_id = :s AND idempotency_key = :k"
            ),
            {"u": user_id, "s": show_id, "k": idem_key},
        )
    ).mappings().first()
    if existing is None:
        return None
    if existing["request_hash"] != req_hash:
        raise Decline("idempotent-mismatch", "Idempotency key was already used with a different seat list")
    rows = (
        await s.execute(
            text(
                "SELECT st.seat_code FROM reservation_seats rs JOIN seats st ON st.id = rs.seat_id "
                "WHERE rs.reservation_id = :r ORDER BY st.seat_code"
            ),
            {"r": existing["id"]},
        )
    ).all()
    return _payload(existing["id"], show_id, user_id, [r[0] for r in rows] or codes, existing["amount_paise"], existing["status"]), True


async def _precheck(user_id, show_id, meta, codes, req_hash, idem_key):
    """Lock-free fast path. Answers replays and obvious declines WITHOUT taking row locks or a transaction,
    so a stampede on a taken hot seat never queues behind row locks. It is an optimisation only: anything it lets
    through is still decided atomically (locks + guarded UPDATE) in _reserve_tx. Order mirrors the tx:
    idempotency -> per-user limit -> seat availability."""
    n = len(codes)
    async with db_gate, SessionLocal() as s:
        found = await _existing_result(s, user_id, show_id, idem_key, req_hash, codes)
        if found:
            return found
        held = (
            await s.execute(
                text("SELECT seats_held FROM user_show_quota WHERE user_id = :u AND show_id = :s"),
                {"u": user_id, "s": show_id},
            )
        ).scalar()
        limit = meta["per_user_limit"]
        if n > limit or (held or 0) + n > limit:
            raise Decline("per-user-limit", f"At most {limit} seats per user for this show", details={"limit": limit})
        rows = (
            await s.execute(
                text("SELECT seat_code, state FROM seats WHERE show_id = :s AND seat_code = ANY(:codes)"),
                {"s": show_id, "codes": codes},
            )
        ).all()
        if len(rows) != n:
            known = {r[0] for r in rows}
            raise Decline("invalid-seats", "Unknown seat(s) for this show", status=400,
                          details={"unknown": [c for c in codes if c not in known]})
        taken = [r[0] for r in rows if r[1] != "available"]
        if taken:
            raise Decline("seat-taken", "One or more requested seats are already taken", details={"taken": taken})
    return None


async def _reserve_tx(s, user_id, show_id, meta, codes, req_hash, idem_key):
    rid = uuid.uuid4()
    n = len(codes)
    amount = meta["price_paise"] * n  # integer paise

    # 1) idempotency claim (unique (user_id, show_id, idempotency_key)); concurrent same-key requests block here
    claimed = (
        await s.execute(
            text(
                "INSERT INTO reservations (id, user_id, show_id, idempotency_key, request_hash, status, amount_paise) "
                "VALUES (:id, :u, :s, :k, :h, 'confirmed', :amt) "
                "ON CONFLICT (user_id, show_id, idempotency_key) DO NOTHING RETURNING id"
            ),
            {"id": rid, "u": user_id, "s": show_id, "k": idem_key, "h": req_hash, "amt": amount},
        )
    ).first()
    if claimed is None:
        found = await _existing_result(s, user_id, show_id, idem_key, req_hash, codes)
        if found is None:  # claimant rolled back between our statements; let the caller retry
            raise RetryTransaction()
        return found

    # 2) per-user limit: conditional UPDATE on the (user, show) quota row (its row lock serialises this user)
    limit = meta["per_user_limit"]
    if n > limit:
        raise Decline("per-user-limit", f"At most {limit} seats per user for this show", details={"limit": limit})
    await s.execute(
        text("INSERT INTO user_show_quota (user_id, show_id, seats_held) VALUES (:u, :s, 0) ON CONFLICT DO NOTHING"),
        {"u": user_id, "s": show_id},
    )
    ok = (
        await s.execute(
            text(
                "UPDATE user_show_quota SET seats_held = seats_held + :n "
                "WHERE user_id = :u AND show_id = :s AND seats_held + :n <= :lim RETURNING seats_held"
            ),
            {"n": n, "u": user_id, "s": show_id, "lim": limit},
        )
    ).first()
    if ok is None:
        raise Decline("per-user-limit", f"At most {limit} seats per user for this show", details={"limit": limit})

    # 3) lock requested seats in deterministic (sorted) order; blocking, so contenders queue and then see the result
    seat_rows = (
        await s.execute(
            text(
                "SELECT id, seat_code, state FROM seats WHERE show_id = :s AND seat_code = ANY(:codes) "
                "ORDER BY seat_code FOR UPDATE"
            ),
            {"s": show_id, "codes": codes},
        )
    ).all()
    if len(seat_rows) != n:
        known = {r[1] for r in seat_rows}
        raise Decline("invalid-seats", "Unknown seat(s) for this show", status=400,
                      details={"unknown": [c for c in codes if c not in known]})
    if any(r[2] != "available" for r in seat_rows):
        raise Decline("seat-taken", "One or more requested seats are already taken",
                      details={"taken": [r[1] for r in seat_rows if r[2] != "available"]})

    # 4) guarded conditional UPDATE — the atomic decision
    ids = [r[0] for r in seat_rows]
    updated = (
        await s.execute(
            text(
                "UPDATE seats SET state = 'confirmed', owner_user_id = :u, reservation_id = :r, updated_at = now() "
                "WHERE id = ANY(:ids) AND state = 'available' RETURNING id"
            ),
            {"u": user_id, "r": rid, "ids": ids},
        )
    ).all()
    if len(updated) != n:
        metrics.RACES.inc()
        log(logger, "race_condition_detected", level=logging.ERROR, stage="guarded_update", show_id=str(show_id))
        raise Decline("seat-taken", "One or more requested seats are already taken")

    # 5) DB-level backstop: partial unique index rejects a second active claim on the same seat
    try:
        await s.execute(
            text("INSERT INTO reservation_seats (reservation_id, seat_id) SELECT :r, unnest(CAST(:ids AS bigint[]))"),
            {"r": rid, "ids": ids},
        )
    except IntegrityError:
        metrics.RACES.inc()
        log(logger, "race_condition_detected", level=logging.ERROR, stage="unique_backstop", show_id=str(show_id))
        raise Decline("seat-taken", "One or more requested seats are already taken")
    return _payload(rid, show_id, user_id, codes, amount, "confirmed"), False


async def cancel(user_id: uuid.UUID, reservation_id: uuid.UUID) -> dict:
    for attempt in range(4):
        try:
            async with db_gate, SessionLocal() as s:
                async with s.begin():
                    return await _cancel_tx(s, user_id, reservation_id)
        except DBAPIError as e:
            if _sqlstate(e) in RETRYABLE_SQLSTATES and attempt < 3:
                continue
            raise
    raise ApiError(503, "SERVICE_UNAVAILABLE", "Please retry")


async def _cancel_tx(s, user_id, reservation_id):
    r = (
        await s.execute(
            text("SELECT id, user_id, show_id, status FROM reservations WHERE id = :id FOR UPDATE"),
            {"id": reservation_id},
        )
    ).mappings().first()
    if r is None:
        raise ApiError(404, "RESERVATION_NOT_FOUND", "Reservation not found")
    if r["user_id"] != user_id:
        raise ApiError(403, "FORBIDDEN", "You can only cancel your own reservations")
    if r["status"] == "cancelled":
        return {"message": "Reservation already cancelled", "reservation_id": str(reservation_id), "status": "cancelled"}

    n = (
        await s.execute(
            text("SELECT count(*) FROM reservation_seats WHERE reservation_id = :r AND released_at IS NULL"),
            {"r": reservation_id},
        )
    ).scalar_one()
    # quota row first (same order as reserve)
    await s.execute(
        text("UPDATE user_show_quota SET seats_held = GREATEST(seats_held - :n, 0) WHERE user_id = :u AND show_id = :s"),
        {"n": n, "u": user_id, "s": r["show_id"]},
    )
    await s.execute(
        text("SELECT id FROM seats WHERE reservation_id = :r ORDER BY seat_code FOR UPDATE"), {"r": reservation_id}
    )
    # guard: only release seats still owned by THIS reservation/user — can never resurrect another user's seat
    await s.execute(
        text(
            "UPDATE seats SET state = 'available', owner_user_id = NULL, reservation_id = NULL, "
            "hold_expiry_time = NULL, updated_at = now() "
            "WHERE reservation_id = :r AND owner_user_id = :u AND state IN ('confirmed','held')"
        ),
        {"r": reservation_id, "u": user_id},
    )
    await s.execute(
        text("UPDATE reservation_seats SET released_at = now() WHERE reservation_id = :r AND released_at IS NULL"),
        {"r": reservation_id},
    )
    await s.execute(
        text("UPDATE reservations SET status = 'cancelled', updated_at = now() WHERE id = :r"), {"r": reservation_id}
    )
    metrics.CANCELLED.inc()
    log(logger, "reservation_cancelled", action="cancel", reservation_id=str(reservation_id), seats=n)
    return {"message": "Reservation cancelled", "reservation_id": str(reservation_id), "status": "cancelled"}
