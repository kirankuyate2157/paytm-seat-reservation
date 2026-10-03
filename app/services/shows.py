import uuid

from sqlalchemy import text

from app.core.errors import ApiError
from app.db.session import SessionLocal, db_gate

# Shows are immutable after creation (price/limit/total), so caching them per process is safe.
_show_cache: dict[uuid.UUID, dict] = {}


async def get_show_meta(show_id: uuid.UUID) -> dict:
    cached = _show_cache.get(show_id)
    if cached:
        return cached
    async with db_gate, SessionLocal() as s:
        row = (
            await s.execute(
                text("SELECT id, name, total_seats, price_paise, per_user_limit FROM shows WHERE id = :id"),
                {"id": show_id},
            )
        ).mappings().first()
    if not row:
        raise ApiError(404, "SHOW_NOT_FOUND", "Show not found")
    meta = dict(row)
    _show_cache[show_id] = meta
    return meta


async def create_show(name: str, seats: list[str], price_paise: int, per_user_limit: int) -> dict:
    show_id = uuid.uuid4()
    async with db_gate, SessionLocal() as s:
        async with s.begin():
            await s.execute(
                text(
                    "INSERT INTO shows (id, name, total_seats, price_paise, per_user_limit) "
                    "VALUES (:id, :name, :n, :price, :lim)"
                ),
                {"id": show_id, "name": name, "n": len(seats), "price": price_paise, "lim": per_user_limit},
            )
            await s.execute(
                text("INSERT INTO seats (show_id, seat_code) SELECT :id, unnest(CAST(:codes AS varchar[]))"),
                {"id": show_id, "codes": seats},
            )
    return {
        "id": str(show_id),
        "name": name,
        "price_paise": price_paise,
        "per_user_limit": per_user_limit,
        "seats": [{"seat_code": c, "state": "available"} for c in seats],
        "available": len(seats),
        "held": 0,
        "confirmed": 0,
        "total": len(seats),
    }


async def show_state(show_id: uuid.UUID, include_seats: bool = True) -> dict:
    meta = await get_show_meta(show_id)
    counts = {"available": 0, "held": 0, "confirmed": 0}
    seats_out = None
    async with db_gate, SessionLocal() as s:
        # Each branch is ONE statement => one consistent snapshot (counts always sum to total).
        if include_seats:
            rows = (
                await s.execute(
                    text("SELECT seat_code, state FROM seats WHERE show_id = :id ORDER BY seat_code"),
                    {"id": show_id},
                )
            ).all()
            seats_out = [{"seat_code": r[0], "state": r[1]} for r in rows]
            for r in rows:
                counts[r[1]] += 1
        else:
            rows = (
                await s.execute(
                    text("SELECT state, count(*) FROM seats WHERE show_id = :id GROUP BY state"), {"id": show_id}
                )
            ).all()
            for state, n in rows:
                counts[state] = n
    total_counted = sum(counts.values())
    out = {
        "id": str(show_id),
        "name": meta["name"],
        "price_paise": meta["price_paise"],
        "per_user_limit": meta["per_user_limit"],
        **counts,
        "total": meta["total_seats"],
        "invariant_ok": total_counted == meta["total_seats"],
    }
    if seats_out is not None:
        out["seats"] = seats_out
    return out
