"""Idempotent seeder: admin + demo users + demo show. Safe to run on every boot."""
import asyncio

from sqlalchemy import text

from app.core.config import settings
from app.core.security import hash_password
from app.db.session import SessionLocal, engine


async def main() -> None:
    async with SessionLocal() as s:
        async with s.begin():
            users = [(settings.admin_email, settings.admin_password, "admin")]
            if settings.seed_demo:
                users += [(f"demo{i}@example.com", "demo12345", "user") for i in range(1, 6)]
            for email, pw, role in users:
                exists = (await s.execute(text("SELECT 1 FROM users WHERE email = :e"), {"e": email})).first()
                if not exists:
                    await s.execute(
                        text("INSERT INTO users (email, password_hash, role) VALUES (:e, :p, :r) ON CONFLICT DO NOTHING"),
                        {"e": email, "p": await hash_password(pw), "r": role},
                    )
            if settings.seed_demo:
                has_show = (await s.execute(text("SELECT 1 FROM shows WHERE name = 'demo-show'"))).first()
                if not has_show:
                    codes = [f"{row}{n}" for row in "ABCDEFGHIJ" for n in range(1, 11)]
                    sid = (
                        await s.execute(
                            text("INSERT INTO shows (name, total_seats, price_paise, per_user_limit) "
                                 "VALUES ('demo-show', :n, 25000, :l) RETURNING id"),
                            {"n": len(codes), "l": settings.default_per_user_limit},
                        )
                    ).scalar_one()
                    await s.execute(
                        text("INSERT INTO seats (show_id, seat_code) SELECT :id, unnest(CAST(:c AS varchar[]))"),
                        {"id": sid, "c": codes},
                    )
    await engine.dispose()
    print("seed complete")


if __name__ == "__main__":
    asyncio.run(main())
