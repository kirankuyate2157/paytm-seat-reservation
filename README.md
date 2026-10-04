# Seat Reservation at Scale

FastAPI + PostgreSQL (SQLAlchemy 2 async/asyncpg, Alembic). Correct under stampede: no double-sell, per-user limit,
idempotent retries, token-derived identity, zero 5xx on domain outcomes. See `WRITEUP.md` for the design and
`CHECKPOINT.md` for build status / TODO.

## Live deployment
- Service: https://paytm-seat-reservation-xxch.onrender.com
- API docs: /docs  |  Health: /health/live, /health/ready  |  Metrics: /metrics
- Admin (for POST /shows): `admin@example.com` / `Paytm-Seats-2026`
- Users: `POST /auth/signup` then use the `access_token` as a Bearer token

## Run the burst against the live service
    pip install httpx
    ./burst.sh https://paytm-seat-reservation-xxch.onrender.com
    # knobs: USERS TOTAL SEATS HOT_REQUESTS CONCURRENCY ADMIN_EMAIL ADMIN_PASSWORD

### Latest burst result (live)
<paste the output here: outcome distribution, reconciliation, RESULT line>

### Logs
```~/myWork/seat-reservation$ ./burst.sh
Burst against http://localhost:8000  (run 05500c65)
show e7d029ef-f825-4bc4-9879-c7f4461ef7f4: 2000 seats, per-user limit 4
604 users signed up

[1] hot-seat storm: 500 users -> seat A12
  [PASS] exactly one 201 for A12 (got 1)
  [PASS] all others 409 seat-taken 

[2] idempotent retry storm: 50x same key, same seat
  [PASS] all 50 returned 201 with ONE reservation id (ids=1)
  [PASS] exactly 1 non-replay 

[3] same key, different seats -> 409
  [PASS] idempotent-mismatch 409 (got 409 idempotent-mismatch)

[4] per-user limit race: 1 user, 10 parallel reserves, limit 4
  [PASS] exactly 4 confirmed (got 4)
  [PASS] 6 declined per-user-limit 

[5] multi-seat all-or-nothing under opposing lock order
  [PASS] exactly one multi-seat winner, no 5xx 
  [PASS] no partial booking (2 of D1-D3 confirmed, 1 available) (['confirmed', 'confirmed', 'available'])

[6] identity + cancel rules
  [PASS] spoofed body user_id ignored (token user wins) 
  [PASS] other user cancel -> 403 
  [PASS] no token -> 401 
  [PASS] owner cancel -> 200 
  [PASS] released seat re-bookable by another user 
  [PASS] re-cancel never resurrects seat now owned by someone else 

[7] full stampede: ~20000 mixed requests (hot seats + retries + duplicates)
  19387 requests in 158.3s (122 req/s)

=== Burst Results ===
- confirmed: 1054
- idempotent-replay: 55
- cancelled: 1
- Declined (idempotent-mismatch): 1
- Declined (per-user-limit): 2586
- Declined (seat-taken): 16304
- 5xx errors: 0   rate-limited(429): 0   client transport errors: 0 {}

=== Reconciliation ===
- Available: 946
- Held: 0
- Confirmed: 1054
- Total: 2000
  [PASS] available + held + confirmed == total_seats (2000 vs 2000)
  [PASS] zero 5xx in whole burst (client-observed) 
  [PASS] zero client transport errors (connection dropped / timeout) ({})
  [PASS] server-side http_5xx_total delta == 0 
  [PASS] no seat confirmed twice (client view) 
  [PASS] DB confirmed == non-replay 201 seats minus cancelled (1054 vs 1054)
  [PASS] metrics: confirmed counter delta == confirmed 201s (1054.0 vs 1054)
  [PASS] metrics: race_conditions_detected == 0 
  [PASS] metrics: seats_confirmed gauge == API confirmed (1054.0 vs 1054)

Total wall time 170.7s
RESULT: ALL CHECKS PASSED
```

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
# paytm-seat-reservation
check http://localhost:8000/docs  openAPI docs of fastapi for testing 
I thought i will add Ui as well but since it was load and scaling related so not added react frontend for now skipped..
<img width="1920" height="15736" alt="image" src="https://github.com/user-attachments/assets/09051ff2-8369-443b-86ef-eeaa294939c4" />
