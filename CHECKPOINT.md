# CHECKPOINT — Seat Reservation (Paytm Backend Round 1)

Update this file at the end of every work session. Status: DONE / IN PROGRESS / TODO.
Last updated: 2026-10-03

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

## DONE (code written AND exercised against real PostgreSQL 16 locally)
- [x] Repo skeleton, requirements, config (pydantic), JSON logging + correlation id middleware
- [x] Alembic async env + migration 0001 (users, shows, seats, reservations, reservation_seats, user_show_quota, audit_logs)
- [x] Auth: signup/login, JWT access token (Bearer or HttpOnly cookie), roles admin/user, token-only identity
- [x] POST /shows, POST /shows/{id}/reserve, POST /reservations/{id}/cancel, GET /shows/{id}, /health/live, /health/ready, /metrics
- [x] Prometheus metrics; seat gauges computed from DB at scrape time
- [x] Fail-closed: DB down => ready 503, reserve/login 503 (+Retry-After), live 200; auto-recovers (verified by stopping Postgres)
- [x] Burst script (scripts/burst.py, burst.sh, make burst) with 20+ pass/fail checks + metrics reconciliation
- [x] Seeder, entrypoint (wait DB -> migrate -> seed -> uvicorn), Dockerfile (multi-stage), docker-compose.yml
- [x] README, WRITEUP.md draft, git repo with incremental commits

## NOT YET VERIFIED (be honest in interview/README)
- [ ] `docker compose up` from a clean clone: Dockerfile/compose are written but Docker is NOT available in the build sandbox, so never executed. Run it first thing on your machine.
- [ ] Anything on a public deployment (not deployed yet)

## TODO (priority order)
P0  1. On your machine: git clone -> docker compose up --build -> /health/ready 200 -> ./burst.sh http://localhost:8000
P0  2. Deploy (Render/Railway/Fly) + managed Postgres; set JWT_SECRET, ADMIN_PASSWORD, COOKIE_SECURE=true; run burst vs live URL; paste output in README
P0  3. Finish WRITEUP.md sections marked (AI usage in YOUR words); read the code until you can explain lock order, why not SKIP LOCKED, the precheck re-check
P1  4. Refresh tokens: sessions + refresh_tokens tables (migration 0002), rotation + reuse detection, /auth/refresh, /auth/logout revocation, CSRF double-submit
P1  5. Rate limiting (Redis; per-user + per-IP; 429 + Retry-After). Exclude burst identities or raise limits for the grader
P1  6. Lock down /metrics (network edge / token) for prod; set CORS origins
P2  7. Audit log writer (background, off the hot path) + admin/self read endpoint
P2  8. TTL holds + expiry sweeper (held state already in schema)
P2  9. pytest unit tests per requirement (burst smoke test exists in tests/)
P3 10. Venues/halls/layouts/events/payments/outbox (Tier 2 schema), Redis token cache, password reset/verify
P3 11. Prometheus multiprocess mode, Grafana dashboard, alert rules file
P3 12. React UI (not graded)

## BUGS FOUND BY THE BURST AND FIXED (good interview stories)
1. Precheck TOCTOU: a retry could pass the idempotency check, then see its own (just-committed) seat as taken and get a false 409.
   Fix: re-check the idempotency key before trusting any fast-path decline.
2. Client ReadErrors at 1000 concurrent connections: uvicorn default keep-alive (5s) closed idle sockets. Fix: --timeout-keep-alive 75.
3. OSError (connection refused) surfaced as 500 when DB was down. Fix: map OSError/TimeoutError to 503.
4. Burst script: scenario users shared with random traffic (limit counts polluted); cookie leakage between users. Fixed in the script.

## TEST LOG (local: 1 vCPU sandbox, Postgres 16 on same box, client on same box)
- Full burst, 2000 seats, 604 users, ~20k requests, run 75c74322: ALL CHECKS PASSED
  confirmed 1051, idempotent-replay 70, declined seat-taken 16353, per-user-limit 2525, idempotent-mismatch 1,
  5xx 0, client transport errors 0, race_conditions_detected 0.
  Reconciliation: available 946 + held 0 + confirmed 1054 = 2000. DB confirmed == client-observed; metrics counter and gauge match.
  Throughput ~100 req/s in the stampede phase (single process, single core, shared with the load generator).
- Hot seat A12: 500 concurrent -> exactly 1x 201, 499x 409 seat-taken.
- DB-down drill: stop Postgres -> ready 503, reserve 503, login 503, live 200, metrics 200; start Postgres -> recovered, reserve 201.
- Medium bursts x3 after the precheck fix: all passed.
