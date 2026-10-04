You are a senior full-stack systems engineer building a production-grade concurrent reservation system under extreme load conditions.
 
Your task: Build, deploy, and operate a seat reservation service for Paytm's backend interview Round 1. The service must handle thousands of concurrent reservation attempts with zero correctness violations, then be deployed live with full observability. Design the system to scale horizontally from day one, with every layer (auth, database, caching, file storage, observability) built to accommodate growth without architectural rework.
 
**Core Constraint — Correctness Under Load:**
When 20,000 concurrent requests storm your service (many targeting the same hot seats, many retrying with identical idempotency keys), your system must guarantee:
1. No seat is ever confirmed to two different users — exactly one 201, rest get clean 409s
2. Zero 5xx errors across the entire burst (all declines are 4xx domain outcomes, never server failures)
3. Reconciliation invariant holds to the unit: available + held + confirmed == total_seats, before/during/after burst
4. Idempotent retries: same idempotency key produces one reservation; same key with different seat list → 409
5. Per-user seat limit enforced atomically (limit=4 means a user firing 10 parallel reserves lands exactly 4 held)
6. Identity from auth token only — no spoofed user escalation; users can only cancel their own holds
 
**Stack:** FastAPI + React + PostgreSQL + Docker. Use Redis for caching, distributed locks, and background jobs as needed. Architecture must be production-scalable and horizontally distributable.
 
## Database Schema Design
 
Build the schema for horizontal scalability and atomicity. Design must support:
 
- **Seats table:** Encode seat state atomically with columns: `id`, `show_id`, `seat_code`, `state` (available/held/confirmed), `held_by_user_id`, `confirmed_user_id`, `hold_expiry_time`, `created_at`, `updated_at`. Use row-level locking for deterministic ordering in multi-seat requests.
- **Reservations table:** Link user + seats + idempotency key: `id`, `user_id`, `show_id`, `idempotency_key`, `seat_ids` (JSON array), `status` (confirmed/cancelled), `amount_paise`, `created_at`, `updated_at`. Unique constraint on `(user_id, show_id, idempotency_key)` to prevent duplicate reservations.
- **Shows table:** `id`, `name`, `total_seats`, `price_paise`, `created_at`.
- **Users table:** `id`, `email`, `created_at`. Scalable for RBAC and audit trails.
- **Audit logs table:** `id`, `user_id`, `action`, `resource`, `resource_id`, `details`, `ip_address`, `timestamp`. Enables compliance and debugging under load.
 
Ensure all tables are normalized, indexed on hot columns (`show_id`, `user_id`, `idempotency_key`, `state`), and partitionable for horizontal scaling. Document why each index exists.
 
## Authentication, Authorization & RBAC
 
Design a reusable, scalable auth system:
 
- **Token-based identity:** Extract `user_id` and `role` from auth token in middleware. No request body field can override identity.
- **RBAC layers:** Define roles explicitly — admin (create shows, view metrics), user (reserve, cancel own reservations), guest (read-only). Middleware enforces role-based access on every endpoint.
- **Token validation:** Cache token validation results in Redis with TTL to reduce DB load under concurrency. Invalidate on logout.
- **Permission checks:** Enforce at endpoint level — POST /shows requires admin; POST /reservations/{id}/cancel requires user owns reservation.
- **Audit trail:** Log every auth success/failure, every role-based decision, every permission denial with correlation ID and user context.
 
Make auth pluggable — JWT, OAuth2, or API key should be swappable without changing business logic.
 
## API Endpoints — Build Exactly These
 
All responses must follow consistent error format: `{ "error": { "code": "ERROR_CODE", "message": "...", "details": {...} }, "correlation_id": "..." }`. Success responses must include `correlation_id`.
 
1. **POST /shows** (admin) — Create show
   - Body: `{ "name": "friday-night", "seats": ["A1","A2","A3"], "price_paise": 25000 }`
   - Response 201: `{ "id": "...", "name": "...", "seats": [...with state], "price_paise": 25000, "correlation_id": "..." }`
   - All seats start in "available" state
 
2. **POST /shows/{id}/reserve** (authenticated user) — Reserve seat(s)
   - Body: `{ "seats": ["A12"], "idempotency_key": "unique-key-abc" }`
   - Identity sourced from auth token, never from request body
   - Response 201 (success): `{ "reservation_id": "...", "show_id": "...", "user_id": "...", "seats": ["A12"], "amount_paise": 25000, "status": "confirmed", "correlation_id": "..." }`
   - Response 409 (decline): `{ "error": { "code": "RESERVATION_DECLINED", "reason": "seat-taken|per-user-limit|idempotent-mismatch", "message": "..." }, "correlation_id": "..." }`
   - **Atomic decision:** Decide and document: partial requests (ask for A12+A13, only A12 free) → all-or-nothing or best-effort? Must hold under concurrency. Enforce atomically at database level with conditional UPDATE or row locks. No application-level logic can race.
 
3. **POST /reservations/{id}/cancel** (user) — Release / expire hold
   - Only the reservation owner can cancel
   - Cancelled seats return to "available" and become re-bookable
   - Must never resurrect a seat already confirmed to another user
   - Response 200: `{ "message": "Reservation cancelled", "correlation_id": "..." }`
 
4. **GET /shows/{id}** — Show state
   - Response includes per-seat status (available / held / confirmed), counts, and total
   - Invariant check: available + held + confirmed == total_seats
   - Example: `{ "id": "...", "name": "...", "seats": [...], "available": 100, "held": 20, "confirmed": 50, "total": 170, "correlation_id": "..." }`
 
5. **GET /health/live** — Liveness endpoint (always 200 if process alive)
 
6. **GET /health/ready** — Readiness endpoint (200 only if DB reachable; 503 if DB down)
 
## Rate Limiting & Caching
 
- **Rate limiting:** Implement per-user and per-IP rate limits (configurable). Use Redis for distributed rate limit tracking across multiple instances. Return 429 with `Retry-After` header.
- **Caching strategy:** Cache show metadata and seat state in Redis with short TTL (configurable). Invalidate cache on every state change. Cache auth token validation results.
- **Cache consistency:** Ensure cache invalidation on every write. Log cache misses and hits for observability.
 
## Idempotency & Race Condition Handling
 
- **Idempotency storage:** Store `(user_id, show_id, idempotency_key)` + outcome in DB. On retry with same key, return cached outcome without re-executing reservation logic.
- **Same-key-different-body detection:** If retry has same idempotency key but different seat list, return 409 with `idempotent-mismatch` reason. Enforce in database with unique constraint on `(user_id, show_id, idempotency_key)`.
- **Race-free atomicity:** The critical decision (who gets the seat) must live in a single atomic database operation. Use:
  - Conditional UPDATE with row locking: `UPDATE seats SET state='held', held_by_user_id=? WHERE seat_id=? AND state='available' AND xmin=? RETURNING *;`
  - OR unique constraints: prevent double-confirms with `UNIQUE(show_id, seat_code, state='confirmed', confirmed_user_id)`
  - Document your chosen mechanism and why it prevents double-booking under 20K concurrent requests.
- **Multi-seat deterministic ordering:** For requests spanning multiple seats, lock seats in a consistent order (e.g., sorted by seat_id) to prevent deadlocks. Document the ordering strategy.
- **Hold expiry model:** Define explicitly — automatic expiry after configurable TTL (e.g., 10 minutes) with background job, or explicit cancel-only. If auto-expiry, implement with APScheduler or Celery; log every expiry.
 
## Response Format Consistency & Error Handling
 
- **HTTP status codes:** Use 201 (created), 200 (OK), 400 (bad request), 401 (unauthorized), 403 (forbidden), 409 (conflict), 429 (rate limited), 503 (service unavailable). Never 5xx for domain logic errors.
- **Error response format:** All errors (4xx and 5xx) return:
  ```json
  {
    "error": {
      "code": "ERROR_CODE",
      "message": "Human-readable message",
      "details": { "field": "value", ... }
    },
    "correlation_id": "uuid"
  }
  ```
- **Validation errors:** Return 400 with field-level details: `{ "error": { "code": "VALIDATION_ERROR", "details": { "seats": "must be non-empty" } }, ... }`
- **Concurrency conflicts:** Return 409 with reason label (seat-taken, per-user-limit, idempotent-mismatch). Metrics track each reason separately.
 
## File Storage & Scalability
 
- **File support:** Design to support disk (local) and S3 (cloud) seamlessly. Abstract storage behind a pluggable interface.
- **Configuration:** Use environment variables to switch: `FILE_STORAGE=s3|local`. If S3, configure bucket, region, credentials via env. If local, configure base path.
- **Scalability:** Local storage for dev; S3 for production horizontal scaling (shared file access across instances).
- **Not required for core reservation flow** but document the abstraction for future features (receipts, exports, etc.).
 
## Decorators, Middleware & ORM
 
- **Middleware stack:** Auth (token extraction), correlation ID (generate and inject into all logs/responses), rate limiting, request/response logging, error handling, CORS.
- **Decorators:** Use `@require_role("admin")`, `@require_auth()`, `@rate_limit()`, `@idempotent()` to keep endpoint code clean.
- **ORM:** Use SQLAlchemy with async drivers (`asyncpg` for PostgreSQL). Keep DB queries explicit and indexed. Avoid N+1 queries.
- **Migrations:** Use Alembic for schema versioning. Track all migrations in version control. Document each migration's purpose.
 
## Configuration & Environment Management
 
- **Config management:** Use Pydantic `Settings` or similar. Load from `.env` (dev) and environment variables (prod). Support multiple environments: dev, staging, prod.
- **Configurable settings:** DB connection pool size, Redis TTL, rate limit thresholds, hold expiry TTL, log level, metrics scrape interval.
- **Database connection:** Connection pooling must be tuned for load. Document pool size rationale for 20K concurrent requests.
 
## Seeders & Initial Data
 
- **Database seeders:** Create initial data (test shows, test users, test reservations) for local dev and staging. Run seeders idempotently on every fresh DB init.
- **Docker entrypoint:** Migrations run → seeders run → service starts.
 
## Docker & Containerization
 
- **Dockerfile:** Multi-stage build (dependencies → app). Minimal production image. Health checks built in.
- **docker-compose.yml:** Define FastAPI service, PostgreSQL, Redis, all with proper networking, volumes, and env files.
  - FastAPI: port 8000, depends on PostgreSQL and Redis
  - PostgreSQL: port 5432, persistent volume, init scripts for schema
  - Redis: port 6379, persistent volume (for load testing)
- **Dev vs Production:** Separate docker-compose files or conditional config:
  - Dev: debug mode, live reload, seed data, no resource limits
  - Prod: no debug, optimized image, resource limits, health checks strict
- **Volume mounts:** Source code (dev), migrations (both), env files (both)
- **Networking:** Services communicate by name. Database and cache are internal; FastAPI exposed on 8000.
- **Fresh start guarantee:** `git clone <repo>` → `docker-compose up` → service runs on port 8000, DB migrations run, seeders populate, `/health/ready` returns 200.
 
## Observability, Monitoring & Alerting
 
Design observability from day one to catch correctness failures at scale.
 
### Prometheus Metrics (Minimum)
 
- Counter `reservations_confirmed_total` (labels: `reason` for outcome type)
- Counter `reservations_declined_total` (labels: `reason`: seat-taken, per-user-limit, idempotent-replay, invalid-seats, etc.)
- Gauge `seats_available` (labels: `show_id`)
- Gauge `seats_held` (labels: `show_id`)
- Gauge `seats_confirmed` (labels: `show_id`)
- Gauge `idempotent_replays_total` (how many retries returned cached result)
- Counter `race_conditions_detected` (if any detected, metric increments; log detail)
- Histogram `reservation_latency_seconds` (labels: `outcome`)
- Gauge `auth_token_cache_hits` (for caching layer visibility)
- All metrics must reconcile with live API state at `/shows/{id}`
 
**Reconciliation Alarm:** Implement periodic check: fetch all metrics, fetch current DB state, verify `confirmed_total == confirmed_gauge + ..., available + held + confirmed == total_seats`. Log divergence as ERROR with correlation ID.
 
### Structured Logging
 
- **Every log line must include:** correlation_id, user_id (if authenticated), action, outcome, timestamps.
- **Format:** JSON for machine parsing. Example:
  ```json
  {
    "timestamp": "2024-01-15T10:30:45Z",
    "correlation_id": "abc-123",
    "level": "INFO",
    "action": "reservation_attempt",
    "user_id": "user-456",
    "show_id": "show-789",
    "seats": ["A12"],
    "outcome": "confirmed",
    "latency_ms": 45
  }
  ```
- **Critical actions to log:** reservation attempt (with seats and user), outcome (confirmed/declined with reason), per-user limit check (allowed/rejected), idempotent replay detection, cache hit/miss, auth success/failure, DB errors.
- **Public access:** Logs must be accessible (docker logs, file export, or live streaming via Prometheus/Loki). Document how to tail logs during burst testing.
 
### Audit & Compliance
 
- **Audit table:** Log every user action: who did what, when, on which resource, from which IP, with which outcome.
- **Retention:** Keep audit logs for compliance; rotate old logs to archive storage.
- **Access control:** Only admins can view full audit logs; users can view their own activity.
 
### Metrics Endpoint
 
- Expose `/metrics` for Prometheus scrape. Return all counters and gauges in Prometheus text format.
- Document scrape interval (e.g., 15s for development, 30s for production).
 
### Alerts (Document, Not Implement)
 
Define what should page on-call at 2am:
- `reservations_confirmed_total` vs DB state divergence > 0.1%
- Any 5xx error during normal load
- `seats_available + held + confirmed != total_seats`
- Idempotent-key collision (same key, different user) → security alert
- Race condition detected (metric > 0)
- Per-show metrics diverge (gauge vs actual query)
 
## Implementation Sequencing
 
Build in this order to catch issues early:
 
1. **Database schema + migrations + seeders.** Test with fresh docker-compose.
2. **Auth middleware + token validation + RBAC decorators.** Verify identity enforcement.
3. **Core endpoints (POST /reserve, GET /shows, POST /cancel).** Implement atomic decision at DB level. Test single-threaded.
4. **Configuration system.** All settings configurable via env.
5. **Structured logging + correlation IDs.** Every action logged with context.
6. **Prometheus metrics.** Counters/gauges for every outcome.
7. **Rate limiting + caching.** Redis integration.
8. **Health endpoints + liveness/readiness.** Cold-start guarantees.
9. **Docker + docker-compose.** Fresh-clone guarantee.
10. **Burst script.** Load testing with 20K concurrent, hot seats, retries, idempotency.
11. **WRITEUP.md** with all sections (see below).
 
## Burst Script
 
Create `./burst.sh <BASE_URL>` or `make burst`. Must:
 
- Fire ~20,000 concurrent reservations at a fresh show
- Storm hot seats (500+ requests targeting the same seat, e.g., seat A12)
- Include retries with identical idempotency keys (test idempotency under load)
- Print outcome distribution: confirmed count, declined breakdown (by reason), 5xx count
- Print final reconciliation check: available + held + confirmed == total_seats
- Demonstrate your service handles the stampede correctly without crashing or returning 5xx
 
Example output:
```
Burst Results:
- Confirmed: 1000
- Declined (seat-taken): 15000
- Declined (per-user-limit): 2000
- Declined (idempotent-replay): 1000
- 5xx errors: 0
 
Reconciliation Check:
- Available: 9000
- Held: 0
- Confirmed: 1000
- Total: 10000 ✓ PASS
```
 
## Testing & Validation Checklist
 
- [ ] Clean clone: `git clone <repo>` → `docker-compose up` → service runs on port 8000, DB ready
- [ ] Burst script executes: `./burst.sh http://localhost:8000` reproduces stampede, no 5xx, output reconciles
- [ ] Manual hot-seat test: 100 concurrent requests for same seat → exactly one 201, rest 409
- [ ] Idempotency test: fire same key 10 times → 1 reservation created, 9 return identical response
- [ ] Same-key-different-body test: key="abc" with ["A12"] succeeds; retry with key="abc" and ["A13"] → 409
- [ ] Per-user limit test: user with limit=4 fires 10 parallel reserves → lands 4 held, rest 409
- [ ] Partial request test: request ["A12","A13"] with 1 free → confirm your behavior (all-or-nothing or best-effort) holds under concurrency
- [ ] Reconciliation invariant: before/during/after burst, available + held + confirmed == total_seats
- [ ] Metrics endpoint: `/metrics` returns Prometheus-format data; counts match API state
- [ ] Logs: correlation ID present on every line, structured JSON, accessible
- [ ] Cold-start: deploy fresh; hit `/health/ready`; confirm it waits for DB, doesn't 200 until ready
- [ ] Rate limiting: exceed limit → 429 with `Retry-After` header
- [ ] Auth enforcement: unauthenticated request → 401; user tries to cancel another's reservation → 403
 
## WRITEUP.md — Document Your Design
 
Cover all sections:
 
1. **Architecture Overview:** System diagram (ASCII or linked), layers, key components, deployment target.
 
2. **Database Design:** Schema diagram with all tables, indexes, constraints. Explain normalization strategy. Why these indexes? How does it scale horizontally?
 
3. **Atomic Decision Mechanism:** Name your exact approach (conditional UPDATE with row lock? unique constraint? advisory lock? sequence?). Explain why it's race-free under 20K concurrent requests targeting the same seat. Include pseudocode or SQL showing the critical operation.
 
4. **Multi-seat Requests & Deadlock Prevention:** How do you handle requests for multiple seats? What's your row-locking order? Why does it prevent deadlock?
 
5. **Idempotency Implementation:** Where is idempotency key stored (which table)? How exactly-once is enforced? How is same-key-different-body rejected with 409? Include schema excerpt.
 
6. **Hold/Expiry Model:** Explicit cancel or time-boxed auto-expire? If auto-expire, how? If explicit, what triggers cleanup? Explain choice.
 
7. **Consistency vs Availability:** What happens if DB partition occurs mid-burst? Does service fail-open (return available-but-may-fail-to-persist) or fail-closed (return 503)? Why?
 
8. **Concurrency Control & Race Condition Safety:** Prove your atomic decision prevents double-booking. Simulate race scenario; show how your mechanism handles it.
 
9. **Observability & Correctness Verification:** What metrics/logs get you paged at 2am? What indicates a correctness failure? Walk through a scenario where metrics catch a bug.
 
10. **Configuration & Scalability:** How do you scale horizontally? What config changes enable multi-instance deployment? How do Redis and DB connection pooling scale with load?
 
11. **Auth & RBAC:** How is identity enforced? Where does token validation happen? How are role-based decisions made and logged?
 
12. **Error Handling & HTTP Semantics:** Show your error response format. Why does your design avoid 5xx for domain errors?
 
13. **AI Usage:** Be specific and honest. What did you use AI for (scaffolding, debugging, design review, schema generation)? What was directed vs decided by you? Your live extension interview will test depth, so own the decisions.
 
14. **Testing Strategy:** Describe how you validated each correctness requirement (hot-seat test, idempotency test, per-user limit test, etc.). Link to burst script.
 
15. **Next Steps:** What would you add next (caching layer for seat state, read replicas for GET /shows, event sourcing for audit trail, circuit breakers, bulkheads, etc.)? Why? How would they change the architecture?
 
## Deliverables
 
1. Public Git repo with incremental commit history (shows progression of work)
2. Live service URL (tested and working when we test)
3. README.md with burst script instructions, fresh-clone guarantee, deployment steps
4. `/metrics` endpoint accessible (Prometheus scrape format)
5. Logs accessible and documented (docker logs or live export)
6. **WRITEUP.md** (all sections above)
7. Clean architecture: no magic, every layer serves a purpose, every decision justified in WRITEUP.md
 
---
 
**Atomic Decision Design:** The correctness bar hinges on WHERE the atomic decision lives. Read-then-write double-sells under contention. Your design must push the decision into a single atomic operation: conditional update guarded on current state, unique constraint preventing double-holds, deterministic row-lock ordering for multi-seat requests, or equivalent. The live behaviour will reveal your approach — we can see it working.
 
**Scenario Handling — Design for Each:**
 
1. **Hot-seat storm:** 500 users grab seat A12 simultaneously → 1 gets 201, 499 get clean 409
2. **Per-user limit race:** User fires 10 parallel reserve requests on limit=4 show → exactly 4 held, 6 rejected with per-user-limit reason
3. **Idempotent retry storm:** Same user retries reserve 50 times with same key → one reservation in DB, metrics show 1 confirmed + 49 idempotent replays (tracked separately)
4. **Partial multi-seat under load:** User requests ["A12","A13"]; both free → confirm both; A12 taken between read and write → your behavior (all or nothing? best effort?) works atomically
5. **DB failover / partition:** Mid-burst, DB becomes unavailable → service returns 503 or 500? Depends on your choice (fail-open or fail-closed). Document it; don't crash.
6. **Cascading retries:** Client retries with backoff; your idempotency absorbs duplicates without double-booking
 
Start building. The challenge is not just correctness — it's proving correctness under load while we watch.