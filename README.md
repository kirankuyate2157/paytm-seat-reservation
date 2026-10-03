# Seat Reservation at Scale

FastAPI + PostgreSQL (SQLAlchemy 2 async/asyncpg, Alembic). Correct under stampede: no double-sell, per-user limit,
idempotent retries, token-derived identity, zero 5xx on domain outcomes. See `WRITEUP.md` for the design and
`CHECKPOINT.md` for build status / TODO.

## Run (fresh clone)
```
docker compose up --build        # migrations -> seed -> API on :8000
curl localhost:8000/health/ready
```
Seeded: admin `admin@example.com` / `admin12345`, demo users `demo1..5@example.com` / `demo12345`, show `demo-show`.
Local without Docker: `pip install -r requirements-dev.txt`, set `DATABASE_URL`, `alembic upgrade head`, `python -m app.seed`,
`uvicorn app.main:app`.

## API (JSON; errors: `{"error":{"code","message","details","reason?"},"correlation_id"}`)
| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | /auth/signup, /auth/login | – | returns `access_token` (also HttpOnly cookie) |
| POST | /shows | admin | `{name, seats[], price_paise (int), per_user_limit?}` |
| POST | /shows/{id}/reserve | user | `{seats[], idempotency_key}` or `Idempotency-Key` header. 201 / 409 reasons: `seat-taken`, `per-user-limit`, `idempotent-mismatch`; 400 `invalid-seats`. Replay returns the original 201 + `Idempotent-Replay: true` |
| POST | /reservations/{id}/cancel | owner | 403 for non-owner |
| GET | /shows/{id}?include_seats=true | – | per-seat state + counts + `invariant_ok` |
| GET | /health/live, /health/ready | – | ready = DB reachable, else 503 |
| GET | /metrics | – | Prometheus text (restrict at the network edge in prod) |

Multi-seat requests are **all-or-nothing**. Reserve is immediate (seat -> `confirmed`); cancel releases it.

## Burst (the on-sale stampede)
```
./burst.sh https://<your-live-url>          # or: make burst BASE_URL=https://...
# knobs: USERS=600 TOTAL=20000 SEATS=2000 HOT_REQUESTS=500 CONCURRENCY=1000 ADMIN_EMAIL=.. ADMIN_PASSWORD=..
```
It creates a fresh show and users, runs hot-seat storm (500 -> A12), 50x same-key retry, same-key-different-body,
10-parallel per-user-limit, opposing-order multi-seat, spoof/cancel rules, then ~20k mixed requests, and prints the
outcome distribution, the reconciliation (available+held+confirmed==total), and cross-checks against `/metrics`.
Exit code is non-zero if any check fails.

## Metrics & logs
`/metrics`: `reservations_confirmed_total`, `reservations_declined_total{reason}`, `idempotent_replays_total`,
`race_conditions_detected_total` (must be 0), `reservation_latency_seconds{outcome}`, `http_5xx_total`,
`seats_available|held|confirmed{show_id}` (computed from the DB at scrape time). Scrape every 15s.
Logs are JSON on stdout with `correlation_id` (also returned as `X-Request-ID`): `docker compose logs -f api`.

## Config (env)
`DATABASE_URL, JWT_SECRET, ACCESS_TOKEN_TTL_SECONDS, ADMIN_EMAIL, ADMIN_PASSWORD, DB_POOL_SIZE, DB_MAX_OVERFLOW,
DB_POOL_TIMEOUT, DB_CONCURRENCY, DEFAULT_PER_USER_LIMIT, LOG_LEVEL, SEED_DEMO` (see `.env.example`).
