# CHECKPOINT — Seat Reservation (Paytm Backend Round 1)

Update this file at the end of every work session. Status: DONE / IN PROGRESS / TODO.
Last updated: 2026-10-02

## Decisions (locked)
- D1 reserve is immediate: seat available -> confirmed, reservation confirmed. Cancel releases. `held` state + `hold_expiry_time` exist in schema for a future TTL/payment step.
- D2 all-or-nothing for multi-seat requests.
- D3 atomic decision = rows locked in sorted seat_code order (SELECT ... FOR UPDATE, blocking, NOT skip-locked) then guarded UPDATE (... AND state='available'); second layer = partial unique index on reservation_seats(seat_id) WHERE released_at IS NULL.
- D4 per-user limit = row in user_show_quota updated with conditional UPDATE (seats_held + n <= limit).
- D5 lock order everywhere: reservation row -> quota row -> seat rows (sorted). Prevents deadlock between reserve and cancel.
- D6 show counts computed with GROUP BY on seats (no hot counter row).
- D7 declined requests are rolled back and NOT stored; a retry of a declined request re-executes (safe, cannot double-book).
- D8 one uvicorn process per container (metrics are per process); scale with replicas. Prometheus multiprocess mode is a TODO.
- D9 DB trouble => 503 (fail closed), never 500; domain declines => 4xx.

## DONE (written, see status of testing below)
- [x] Repo skeleton, requirements, Dockerfile (multi-stage), docker-compose, entrypoint
- [x] Config (pydantic settings), JSON logging, correlation id middleware
- [x] Alembic async env + migration 0001 (users, shows, seats, reservations, reservation_seats, user_show_quota, audit_logs)
- [x] Auth: signup/login, JWT access token (Bearer or cookie), roles admin/user, token-only identity
- [x] Endpoints: POST /shows, POST /shows/{id}/reserve, POST /reservations/{id}/cancel, GET /shows/{id}, /health/live, /health/ready, /metrics
- [x] Prometheus metrics (confirmed, declined by reason, replays, races, latency, seat gauges from DB at scrape)
- [x] Burst script scripts/burst.py + burst.sh + Makefile
- [x] Seeder (admin + demo users + demo show)

## IN PROGRESS
- [ ] Run everything against a real Postgres and record results (see TEST LOG below)

## TODO (priority order)
P0  1. Fix anything the test run exposes; run burst with 20k requests; paste results into README
P0  2. Clean-clone check: docker compose up from fresh checkout, /health/ready = 200
P0  3. Deploy (Render/Railway/Fly) + managed Postgres; run burst against live URL
P1  4. Refresh tokens: sessions + refresh_tokens tables (migration 0002), rotation with reuse detection, /auth/refresh, /auth/logout, httpOnly cookies + CSRF double-submit
P1  5. Rate limiting (Redis, per-user + per-IP, 429 + Retry-After)
P1  6. WRITEUP.md: finish sections, write AI-usage section honestly in own words
P2  7. Audit log writer (background, off hot path), admin/self audit read endpoint
P2  8. TTL holds + expiry sweeper (uses held state)
P2  9. Redis token/revocation cache; password reset / email verify tables
P3 10. Venues/halls/layouts/events/payments/outbox tables (Tier 2 design)
P3 11. Prometheus multiprocess mode, Grafana dashboard, alert rules file
P3 12. React UI (not graded)

## TEST LOG
(none yet)
