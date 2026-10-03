from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

registry = CollectorRegistry()

CONFIRMED = Counter("reservations_confirmed_total", "Reservations confirmed", registry=registry)
DECLINED = Counter(
    "reservations_declined_total",
    "Reservation requests not executed, by reason (seat-taken, per-user-limit, idempotent-mismatch, "
    "invalid-seats, idempotent-replay = served from stored result)",
    ["reason"],
    registry=registry,
)
REPLAYS = Counter("idempotent_replays_total", "Retries answered from the stored reservation", registry=registry)
RACES = Counter(
    "race_conditions_detected_total",
    "DB backstop caught a double-claim the app-level lock should have prevented (must stay 0)",
    registry=registry,
)
CANCELLED = Counter("reservations_cancelled_total", "Reservations cancelled", registry=registry)
LATENCY = Histogram(
    "reservation_latency_seconds",
    "Reserve latency by outcome",
    ["outcome"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
    registry=registry,
)
HTTP = Counter("http_requests_total", "HTTP requests", ["method", "status"], registry=registry)
HTTP_5XX = Counter("http_5xx_total", "HTTP 5xx responses (must stay 0 on domain outcomes)", registry=registry)
AUTH_FAIL = Counter("auth_failures_total", "Auth failures", ["reason"], registry=registry)

SEATS = Gauge("seats", "Seats by state, computed from the DB at scrape time", ["show_id", "state"], registry=registry)
SEATS_AVAILABLE = Gauge("seats_available", "Available seats", ["show_id"], registry=registry)
SEATS_HELD = Gauge("seats_held", "Held seats", ["show_id"], registry=registry)
SEATS_CONFIRMED = Gauge("seats_confirmed", "Confirmed seats", ["show_id"], registry=registry)


def render() -> bytes:
    return generate_latest(registry)
