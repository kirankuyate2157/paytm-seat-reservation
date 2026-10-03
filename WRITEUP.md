# WRITEUP (draft — sections marked ✍ must be finished in your own words before submitting)

## 1. Atomic decision
Where: one PostgreSQL transaction in `app/services/reservations.py::_reserve_tx`.
1. `SELECT id, seat_code, state FROM seats WHERE show_id=? AND seat_code = ANY(?) ORDER BY seat_code FOR UPDATE`
   — blocking row locks in sorted order. Contenders for the same seat queue on the row lock; after the winner commits
   they re-read the row as `confirmed` and decline.
2. Guarded conditional UPDATE: `UPDATE seats SET state='confirmed', owner_user_id=? ... WHERE id = ANY(?) AND state='available' RETURNING id`.
   If the rows returned != seats requested, roll back (and bump `race_conditions_detected_total`, which must stay 0).
3. DB backstop: partial unique index `reservation_seats(seat_id) WHERE released_at IS NULL` — even a buggy code path
   cannot create two active claims on one seat.
Not `SKIP LOCKED`: skipping a seat locked by an in-flight transaction would decline a seat that might be released
(e.g. that transaction fails the per-user limit) => zero winners for a hot seat. Blocking is what guarantees exactly one 201.
A lock-free **fast path** (`_precheck`) answers replays and obvious declines (seat already confirmed, user at limit)
without locks, so a stampede on a taken seat does not queue on row locks. It is only an optimisation; every request it
lets through is decided atomically by steps 1–3.

## 2. Multi-seat & deadlock
All-or-nothing. Seats are locked in sorted `seat_code` order, and the global lock order is
reservation row -> `user_show_quota` row -> seat rows, in both reserve and cancel. Two requests `[D1,D2]` and `[D3,D2]`
lock D2 in the same relative order, so no cycle can form (tested with opposing orders in the burst). Postgres deadlock
(40P01)/serialization (40001) errors would be retried up to 3 times.

## 3. Idempotency
Table `reservations`, `UNIQUE(user_id, show_id, idempotency_key)` + `request_hash` (sha256 of the sorted seat list).
The transaction first does `INSERT ... ON CONFLICT DO NOTHING`; a concurrent same-key request blocks on the unique index
until the first commits, then reads the stored row: same hash -> original 201 (`Idempotent-Replay: true`), different
hash -> 409 `idempotent-mismatch`. Declined attempts roll back (including the key claim), so a retry of a *declined*
request re-executes — it can never double-book. Known trade-off: the response of a decline is not stored.

## 4. Per-user limit
`user_show_quota(user_id, show_id, seats_held)` updated with `UPDATE ... WHERE seats_held + n <= limit`. The row lock
serialises one user's parallel requests; no advisory locks. Rolls back with the transaction.

## 5. Holds & expiry
Explicit cancel only: reserve confirms immediately; `POST /reservations/{id}/cancel` (owner only) releases with a guard
`WHERE reservation_id=? AND owner_user_id=? AND state IN ('confirmed','held')`, so it can never touch a seat now owned by
someone else. The schema already has `held` + `hold_expiry_time` (+ partial index) for a TTL/payment step (TODO).

## 6. Consistency vs availability
Fail closed. If the DB is unreachable: `/health/ready` -> 503, API errors map to 503 + `Retry-After` — never a 200 that
was not persisted and never a generic 500. Inventory is a CP choice: refusing is safer than overselling.
Backpressure: `DB_CONCURRENCY` semaphore queues work in-process instead of failing on pool timeout.

## 7. Observability / 2am pages
Page on: any `http_5xx_total` increase; `race_conditions_detected_total > 0`; `seats_available+held+confirmed != total`
(`invariant_ok=false` on GET /shows); confirmed-counter vs DB-confirmed divergence; readiness flapping; DB pool/queue
saturation; p99 `reservation_latency_seconds` spike. Logs: JSON with `correlation_id` (= `X-Request-ID`).

## 8. Scaling
Stateless API replicas behind a load balancer; correctness lives in Postgres row locks, so it holds with N replicas.
Pool per replica x replicas must stay under Postgres `max_connections` (PgBouncer in front for more). Hot show = one
partition/shard key (`show_id`). Reads can go to replicas (seat-map lag acceptable; writes always primary).
Metrics are per process (one uvicorn worker per container; scale with replicas).

## 9. Testing (what was actually run)
See `CHECKPOINT.md` TEST LOG and `scripts/burst.py` (hot seat 500->1 winner, 50x same key, same key/different body,
10-parallel per-user limit, opposing-order multi-seat, spoofed identity, cancel/rebook, ~20k mixed requests).

## 10. AI usage ✍
Write this yourself, honestly and specifically: what you directed vs. decided, what you changed or rejected, and which
parts you can explain and extend live (lock order, why not SKIP LOCKED, idempotency claim flow).

## 11. Next steps
Refresh-token rotation + cookies/CSRF, Redis rate limiting, TTL holds + sweeper, audit writer, outbox for notifications,
payments, Prometheus multiprocess/Grafana, read replicas, waiting-room for on-sale spikes.
