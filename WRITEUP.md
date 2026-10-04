# WRITEUP — Seat Reservation at Scale

The one bracketed line at the end of section 12 needs your own dates before you submit. Everything else describes what the code does and what was actually measured.

## 0. Summary
FastAPI + PostgreSQL 16 (SQLAlchemy 2 async / asyncpg, Alembic). All correctness lives in Postgres: row locks in a fixed order, a guarded conditional UPDATE, and unique indexes. The app tier is stateless, so more replicas do not change correctness. Verified locally with `scripts/burst.py`: 500 concurrent requests for one seat gave exactly one 201 and 499 clean 409s, and a ~20k-request mixed stampede gave zero 5xx with `available + held + confirmed == total` to the unit (numbers in section 9).

## 1. The atomic decision
**Where:** one PostgreSQL transaction, `app/services/reservations.py::_reserve_tx`.

1. `SELECT id, seat_code, state FROM seats WHERE show_id=:s AND seat_code = ANY(:codes) ORDER BY seat_code FOR UPDATE` takes blocking row locks in sorted order. Everyone fighting for A12 queues on that row lock. When the winner commits, each waiter re-reads the row as `confirmed` and declines.
2. Guarded conditional UPDATE: `UPDATE seats SET state='confirmed', owner_user_id=:u, reservation_id=:r WHERE id = ANY(:ids) AND state='available' RETURNING id`. If the number of rows returned differs from the number of seats requested, the whole transaction rolls back and `race_conditions_detected_total` increments (it must stay 0).
3. Database backstop: partial unique index `reservation_seats(seat_id) WHERE released_at IS NULL`. A seat can have only one active reservation, even if the application code were wrong.

**Why it is race-free:** a row lock admits one transaction at a time, and the second `state='available'` guard re-checks the row after the lock is acquired. There is no gap between "check" and "write" that another transaction can enter, because the check and the write happen while the same lock is held. Under 20,000 requests the database serialises only the requests that touch the same row; requests for different seats do not block each other.

**Why a read-then-write fails:** two requests read `available`, both write `confirmed`. The unconditional write overwrites the first. Pushing the condition into the UPDATE (or holding the lock from the read) removes that window.

**Why blocking locks and not `SKIP LOCKED`:** skipping a seat locked by an in-flight transaction would decline a seat that might be released a moment later (for example, the lock holder fails the per-user limit and rolls back). A hot seat could end with zero winners. Blocking guarantees exactly one winner.

**Fast path (optimisation only):** `_precheck` answers replays and obvious declines (seat already confirmed, user at limit) without a transaction or row locks, so a stampede on an already-taken seat does not queue on locks. Anything it lets through is still decided by steps 1-3. It had a real bug (section 9): a decline could be wrongly returned to a retry whose first attempt had just committed. The fix re-checks the idempotency key before trusting any decline.

## 2. Multi-seat requests and deadlock
**Behaviour: all-or-nothing.** If any requested seat is unavailable or unknown, nothing is booked and the user gets a 409 (or 400 for unknown seat codes). Best-effort was rejected: it makes the response ambiguous for the client and makes "partial" states possible under concurrency.

**Deadlock avoidance:** seat rows are always locked in sorted `seat_code` order, and the global lock order is **reservation row -> user_show_quota row -> seat rows** in both reserve and cancel. Requests for `[D1,D2]` and `[D3,D2]` both lock D2 in the same relative order, so a wait cycle cannot form. The burst runs 25 of each concurrently and gets exactly one winner. Postgres deadlock (40P01) and serialization (40001) errors would be retried up to three times.

## 3. Idempotency
- **Where:** table `reservations`, `UNIQUE(user_id, show_id, idempotency_key)`, plus `request_hash` (sha256 of the sorted, de-duplicated seat list).
- **Exactly once:** the transaction starts with `INSERT ... ON CONFLICT DO NOTHING` on that key. A concurrent request with the same key blocks on the unique index until the first transaction commits or rolls back. If it committed, the second request reads the stored row.
- **Same key, same body:** returns the original 201 with the original reservation id and an `Idempotent-Replay: true` header. Counted in `idempotent_replays_total`.
- **Same key, different seats:** 409 `idempotent-mismatch`.
- **Key is scoped to user and show,** so one user cannot collide with or probe another user's keys.
- **Declined attempts are not stored.** They roll back, including the key claim. A retry of a declined request re-executes, which is safe because it can never double-book. Trade-off: a decline is recomputed rather than replayed.
- **Lost response:** if the connection drops after commit and before the client sees the 201, the retry with the same key returns the original reservation. That is the property that makes retries safe.

## 4. Per-user limit
`user_show_quota(user_id, show_id, seats_held)` is updated with `UPDATE ... SET seats_held = seats_held + :n WHERE ... AND seats_held + :n <= :limit`. Zero rows updated means `per-user-limit`. The row lock on that one row serialises a single user's parallel requests, so ten parallel reserves with limit 4 end with exactly four. It rolls back with the transaction and is decremented on cancel. No advisory locks and no counting-then-inserting.

## 5. Holds and expiry
**Model: explicit cancel only.** Reserve confirms immediately. `POST /reservations/{id}/cancel` is owner-only (403 otherwise) and releases with a guard: `WHERE reservation_id=:r AND owner_user_id=:u AND state IN ('confirmed','held')`. A cancel can never touch a seat that now belongs to someone else, and cancelling twice is a harmless no-op.
**Why:** the brief returns `status: "confirmed"` on 201 and there is no payment step, so a TTL hold would add a sweeper and expiry races without a user-visible benefit. The schema already has the `held` state, `hold_expiry_time` and a partial index, so TTL holds are an additive change (section 11).

## 6. Consistency vs availability under a partition
**Fail closed (CP).** If the app cannot reach the database, `/health/ready` returns 503 and reserve, login and the other DB-backed endpoints return 503 with `Retry-After`. Liveness stays 200 so the process is not killed. Verified by stopping Postgres mid-run: 503s while down, automatic recovery when it returned.
**Why:** for inventory, refusing is safer than overselling. A client that got a 503 can retry with the same idempotency key.
**Ambiguous commit:** if the connection dies after the commit but before the response, the client cannot know the outcome; the idempotent retry resolves it without a duplicate.
**Failover caveat:** with an asynchronous replica promoted after a primary failure, recently committed bookings could be lost and a seat re-sold. Mitigation: synchronous replication for the primary-replica pair (`synchronous_commit = on` with a synchronous standby) at the cost of write latency. Not configured here (single database).

## 7. Observability and what pages at 2am
**Metrics (`/metrics`):** `reservations_confirmed_total`, `reservations_declined_total{reason}`, `idempotent_replays_total`, `race_conditions_detected_total`, `reservation_latency_seconds{outcome}`, `http_requests_total`, `http_5xx_total`, and `seats{show_id,state}`, `seats_available|held|confirmed{show_id}`. The seat gauges are computed from the database at scrape time, so they reconcile with `GET /shows/{id}` by construction.
**Logs:** JSON on stdout with `correlation_id` (also returned as `X-Request-ID`), `user_id`, action, outcome and latency. Tail with `docker compose logs -f api` (or the platform's log view).
**Page on:**
- `race_conditions_detected_total > 0` (a double-claim reached the guard: correctness bug).
- any increase in `http_5xx_total` during normal load.
- `available + held + confirmed != total` for any show (`invariant_ok=false`).
- confirmed counter diverging from the database count of confirmed seats (beyond cancels).
- `/health/ready` failing or flapping.
- p99 `reservation_latency_seconds` high, or DB connections near the limit.
- a sudden rise in `auth_failures_total` (credential stuffing).
**Example catch:** if a code change let a seat be confirmed twice, the backstop unique index rejects the second claim, `race_conditions_detected_total` increments and logs a `race_condition_detected` ERROR line with the correlation id.

## 8. Scaling horizontally
- App replicas are stateless (JWT auth, no local state). Correctness is in Postgres row locks, so it holds with N replicas.
- Connection math: replicas x (`DB_POOL_SIZE` + `DB_MAX_OVERFLOW`) must stay under Postgres `max_connections`; use PgBouncer (transaction mode) for more. `DB_CONCURRENCY` bounds concurrent transactions per process, so a stampede queues in memory rather than failing on pool timeout.
- A hot show is one contention domain; the shard key is `show_id`.
- Reads (`GET /shows`) can move to a replica (seat-map lag acceptable); reserve and cancel always hit the primary.
- Metrics are per process (one uvicorn worker per container); Prometheus scrapes each replica.

## 9. Testing: what was actually run
`scripts/burst.py` (also `./burst.sh`, `make burst`) against a real Postgres 16, run on a 1-vCPU sandbox where the API, database and load generator shared the machine.
| Requirement | How tested | Result |
|---|---|---|
| Hot seat | 500 concurrent requests for A12 | 1x 201, 499x 409 seat-taken |
| Idempotent retry | same key 50x in parallel | 50x 201, one reservation id, one non-replay |
| Key reuse, different body | same key, different seat | 409 idempotent-mismatch |
| Per-user limit | one user, 10 parallel reserves, limit 4 | exactly 4 confirmed, 6 per-user-limit |
| Multi-seat, opposite order | 25x [D1,D2] vs 25x [D3,D2] | one winner, no partial booking, no 5xx |
| Identity | spoofed `user_id` in body | token user wins |
| Cancel | other user / no token / owner / re-cancel | 403 / 401 / 200 / does not resurrect a re-sold seat |
| Full stampede | ~20k mixed requests (hot seats, retries, duplicate keys) | 0 server 5xx, 0 client errors, race counter 0 |
| Reconciliation | after stampede | 946 + 0 + 1054 = 2000; DB confirmed = client-observed = metrics counter and gauge |
| Dependency failure | stop Postgres | ready/reserve/login 503, live 200, auto-recovery |
Throughput in the stampede phase was about 100 requests/second on that single shared core. That is a sandbox number, not a capacity claim; re-measure on the deployed service.
**Bugs the burst found and fixed:** (1) the fast-path TOCTOU described in section 1; (2) uvicorn's 5-second keep-alive dropped idle connections at 1000 concurrent clients (now 75 s); (3) a refused DB connection surfaced as a 500 (now 503); (4) test-harness issues (shared scenario users, leaked cookies).

## 10. Auth, errors and API semantics
- **Identity:** HS256 JWT (`sub`, `role`, `exp`, `iss`) from the `Authorization: Bearer` header or HttpOnly cookie. Identity is taken only from the verified token; unknown body fields such as `user_id` are ignored. Roles: admin (create shows), user (reserve, cancel own). Passwords use argon2id. Login performs a dummy hash for unknown emails so timing does not reveal whether an account exists.
- **Errors:** `{"error":{"code","message","details","reason?"},"correlation_id"}`. Domain outcomes are 4xx: 400 validation / invalid seats, 401, 403, 404, 409 (`seat-taken`, `per-user-limit`, `idempotent-mismatch`). Dependency trouble is 503. A 5xx is reserved for bugs and outages and is alerting-worthy.
- **No rate limiting by design:** a 429 during the on-sale burst would break "every loser gets a 409". Rate limiting belongs at the edge, with an exemption for the load test.

## 11. Known limitations and next steps
**Limitations (honest):** declines are not stored; metrics are per process; `/metrics` and signup are open (evaluation convenience); HS256 shared secret; no refresh tokens; throughput measured only on one shared core; the Docker setup and the public deployment were not exercised in the environment where the code was written; I list when I ran them in section 12.
**Next, in order:** (1) refresh-token rotation with reuse detection and CSRF for cookie sessions; (2) TTL holds with a sweeper using `FOR UPDATE SKIP LOCKED` on expired holds (safe there, because skipping only delays cleanup); (3) payments with webhook idempotency and an outbox table; (4) Prometheus multiprocess mode, Grafana dashboard and alert rules; (5) PgBouncer, a read replica for seat maps, and synchronous replication for failover safety; (6) a virtual waiting room in front of the on-sale spike; (7) audit log writer off the hot path, partitioned by month.

## 12. AI usage

I used Claude (Anthropic) for most of this, and I want to be upfront about how much. I had about a day, and I used the AI as a fast pair programmer rather than doing it all by hand.

**What I directed.** I picked the stack: FastAPI, PostgreSQL, SQLAlchemy (async) with Alembic, Docker, Redis only if needed. I gave it the full brief and my own requirements doc, and I pushed it toward a production-style design: proper schema, auth, health checks, metrics, a burst script. I also kept asking for the priorities to be written down as a checkpoint file so I could pick the work up again after the session ran out.

**What the AI did.** It wrote most of the code: the schema and the Alembic migration, the reserve and cancel logic, the auth and error handling, the metrics, the burst script, the Dockerfile and compose file, and the first drafts of the README and this write-up. It also ran the burst against a real Postgres in its sandbox and reported the numbers in section 9.

**Decisions that came out of that.** The main design calls were: lock seats in sorted order with a guarded update (instead of read-then-write), all-or-nothing for multi-seat requests, explicit cancel instead of timed holds, fail closed with a 503 when the database is down, and no rate limiting on the reserve path because a 429 would break the "everyone else gets a 409" rule. The AI proposed these with reasons and I went with them. I should say plainly that I did not invent them from scratch.

**What I changed or dropped.** I cut the scope that did not help the graded part (React UI, refresh tokens, extra tables) down to "next steps" so the core would get finished and deployed.

**What testing found.** The burst script caught real bugs in the first version of the code, which is why I trust the final numbers more than a clean first run: a retry could get a wrong "seat taken" from the fast path, idle connections were dropped at 1000 concurrent clients, and a refused database connection came back as a 500 instead of a 503. All three are fixed and described in section 9.

**What I can and cannot claim.** I can explain the lock order, why I used blocking locks and not `SKIP LOCKED`, how the idempotency key claim works, and the fast-path bug and its fix. I have not benchmarked this beyond one small shared machine, so I would not quote the throughput number as real capacity.

**Verification on my side.**  I Tested on My local machine with docker environments and tests..