#!/bin/sh
set -e
python -m scripts.wait_for_db
alembic upgrade head
python -m app.seed
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --backlog 4096 --timeout-keep-alive 75 --no-access-log
