"""Block until the database accepts connections (cold-start safety)."""
import asyncio
import sys
import time

from sqlalchemy import text

from app.db.session import engine


async def main(timeout: int = 90) -> None:
    deadline = time.time() + timeout
    while True:
        try:
            async with engine.connect() as c:
                await c.execute(text("SELECT 1"))
            print("database ready")
            await engine.dispose()
            return
        except Exception as e:  # noqa: BLE001
            if time.time() > deadline:
                print(f"database not reachable: {e}", file=sys.stderr)
                sys.exit(1)
            await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
