"""On-sale stampede against a live URL.   usage: python scripts/burst.py <BASE_URL>
Env: ADMIN_EMAIL, ADMIN_PASSWORD, USERS(600), TOTAL(20000), SEATS(2000), HOT_REQUESTS(500), CONCURRENCY(1000)
Exit code 0 only if every correctness check passes."""
import asyncio
import os
import random
import sys
import time
import uuid
from collections import Counter

import httpx

BASE = (sys.argv[1] if len(sys.argv) > 1 else os.getenv("BASE_URL", "http://localhost:8000")).rstrip("/")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin12345")
USERS = int(os.getenv("USERS", "600"))
TOTAL = int(os.getenv("TOTAL", "20000"))
SEATS = int(os.getenv("SEATS", "2000"))
HOT_REQUESTS = int(os.getenv("HOT_REQUESTS", "500"))
CONC = int(os.getenv("CONCURRENCY", "1000"))
RUN = uuid.uuid4().hex[:8]

checks: list[tuple[str, bool, str]] = []
tally: Counter = Counter()
client_errors: Counter = Counter()
confirmed_codes: list[str] = []      # every seat code confirmed by a non-replay 201
cancelled_codes: list[str] = []
reservations: dict[str, dict] = {}


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")


class Client:
    def __init__(self):
        async def _drop_cookies(response):  # identify by Authorization header only, never by a leftover cookie
            self.http.cookies.clear()

        self.http = httpx.AsyncClient(base_url=BASE, timeout=120, event_hooks={"response": [_drop_cookies]},
                                      limits=httpx.Limits(max_connections=CONC, max_keepalive_connections=CONC))
        self.gate = asyncio.Semaphore(CONC)

    async def call(self, method, path, token=None, json=None, headers=None):
        h = dict(headers or {})
        if token:
            h["Authorization"] = f"Bearer {token}"
        async with self.gate:
            try:
                return await self.http.request(method, path, json=json, headers=h)
            except httpx.HTTPError as e:  # transport-level failure seen by the client (NOT a server 5xx)
                tally["client-error"] += 1
                client_errors[type(e).__name__] += 1
                return httpx.Response(599, json={"error": {"code": "CLIENT", "message": repr(e)}})

    async def reserve(self, show_id, token, seats, key=None, extra=None, record=True):
        key = key or uuid.uuid4().hex
        body = {"seats": seats, "idempotency_key": key, **(extra or {})}
        r = await self.call("POST", f"/shows/{show_id}/reserve", token, body)
        try:
            data = r.json()
        except Exception:
            data = {}
        replay = r.headers.get("Idempotent-Replay") == "true"
        reason = (data.get("error") or {}).get("reason") or (data.get("error") or {}).get("code")
        if r.status_code == 201:
            if replay:
                tally["idempotent-replay"] += 1
            else:
                tally["confirmed"] += 1
                if record:
                    confirmed_codes.extend(data["seats"])
                    reservations[data["reservation_id"]] = data
        elif r.status_code == 599:
            pass  # counted as client-error in call()
        elif r.status_code >= 500:
            tally["5xx"] += 1
        elif r.status_code == 429:
            tally["rate-limited"] += 1
        else:
            tally[f"declined:{reason}"] += 1
        return r.status_code, reason, replay, data


async def main() -> int:
    c = Client()
    t_start = time.time()
    print(f"Burst against {BASE}  (run {RUN})")
    r = await c.call("POST", "/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    if r.status_code != 200:
        print("admin login failed", r.status_code, r.text)
        return 2
    admin = r.json()["access_token"]
    def row_label(r: int) -> str:
        return chr(65 + r) if r < 26 else chr(64 + r // 26) + chr(65 + r % 26)

    seats = [f"{row_label(i // 50)}{i % 50 + 1}" for i in range(SEATS)]  # A1..A50, B1..B50, ... AA1..
    r = await c.call("POST", "/shows", admin, {"name": f"burst-{RUN}", "seats": seats, "price_paise": 25000})
    if r.status_code != 201:
        print("create show failed", r.status_code, r.text)
        return 2
    show = r.json()["id"]
    print(f"show {show}: {SEATS} seats, per-user limit 4")

    # users
    sem = asyncio.Semaphore(40)

    async def signup(i):
        async with sem:
            rr = await c.call("POST", "/auth/signup", json={"email": f"burst-{RUN}-{i}@example.com", "password": "burst-pass-123"})
            return rr.json()

    users = await asyncio.gather(*[signup(i) for i in range(USERS + 4)])  # +4 dedicated scenario users
    tokens = [u["access_token"] for u in users]
    uids = [u["user_id"] for u in users]
    print(f"{USERS + 4} users signed up")
    m0 = await metric_values(c)

    print("\n[1] hot-seat storm: %d users -> seat A12" % HOT_REQUESTS)
    before = len(confirmed_codes)
    res = await asyncio.gather(*[c.reserve(show, tokens[i % USERS], ["A12"]) for i in range(HOT_REQUESTS)])
    wins = [x for x in res if x[0] == 201]
    check("exactly one 201 for A12", len(wins) == 1, f"(got {len(wins)})")
    check("all others 409 seat-taken", all(x[0] == 409 and x[1] == "seat-taken" for x in res if x[0] != 201))

    print("\n[2] idempotent retry storm: 50x same key, same seat")
    key = "retry-" + RUN
    res = await asyncio.gather(*[c.reserve(show, tokens[USERS], ["B1"], key=key) for _ in range(50)])
    ids = {x[3].get("reservation_id") for x in res if x[0] == 201}
    check("all 50 returned 201 with ONE reservation id", len(ids) == 1 and all(x[0] == 201 for x in res), f"(ids={len(ids)})")
    check("exactly 1 non-replay", sum(1 for x in res if not x[2]) == 1)

    print("\n[3] same key, different seats -> 409")
    s, reason, _, _ = await c.reserve(show, tokens[USERS], ["B2"], key=key)
    check("idempotent-mismatch 409", s == 409 and reason == "idempotent-mismatch", f"(got {s} {reason})")

    print("\n[4] per-user limit race: 1 user, 10 parallel reserves, limit 4")
    res = await asyncio.gather(*[c.reserve(show, tokens[USERS + 1], [f"C{i + 1}"]) for i in range(10)])
    n_ok = sum(1 for x in res if x[0] == 201)
    check("exactly 4 confirmed", n_ok == 4, f"(got {n_ok})")
    check("6 declined per-user-limit", sum(1 for x in res if x[1] == "per-user-limit") == 6)

    print("\n[5] multi-seat all-or-nothing under opposing lock order")
    g1 = [c.reserve(show, tokens[10 + i], ["D1", "D2"]) for i in range(25)]
    g2 = [c.reserve(show, tokens[40 + i], ["D3", "D2"]) for i in range(25)]
    res = await asyncio.gather(*(g1 + g2))
    check("exactly one multi-seat winner, no 5xx", sum(1 for x in res if x[0] == 201) == 1 and not any(x[0] >= 500 for x in res))
    st = (await c.call("GET", f"/shows/{show}")).json()
    d = [s["state"] for s in st["seats"] if s["seat_code"] in ("D1", "D2", "D3")]
    check("no partial booking (2 of D1-D3 confirmed, 1 available)", sorted(d) == ["available", "confirmed", "confirmed"], f"({d})")

    print("\n[6] identity + cancel rules")
    s, _, _, body = await c.reserve(show, tokens[USERS + 2], ["G1"], extra={"user_id": uids[USERS + 3], "userId": uids[USERS + 3]})
    check("spoofed body user_id ignored (token user wins)", s == 201 and body["user_id"] == uids[USERS + 2])
    rid = body["reservation_id"]
    r = await c.call("POST", f"/reservations/{rid}/cancel", tokens[USERS + 3])
    check("other user cancel -> 403", r.status_code == 403)
    r = await c.call("POST", f"/reservations/{rid}/cancel")
    check("no token -> 401", r.status_code == 401)
    r = await c.call("POST", f"/reservations/{rid}/cancel", tokens[USERS + 2])
    check("owner cancel -> 200", r.status_code == 200)
    confirmed_codes.remove("G1"); cancelled_codes.append("G1"); tally["cancelled"] += 1
    s, _, _, body2 = await c.reserve(show, tokens[USERS + 3], ["G1"])
    check("released seat re-bookable by another user", s == 201)
    r = await c.call("POST", f"/reservations/{rid}/cancel", tokens[USERS + 2])
    st = (await c.call("GET", f"/shows/{show}")).json()
    g1 = next(x for x in st["seats"] if x["seat_code"] == "G1")["state"]
    check("re-cancel never resurrects seat now owned by someone else", g1 == "confirmed")

    print("\n[7] full stampede: ~%d mixed requests (hot seats + retries + duplicates)" % TOTAL)
    hot = [f"F{i}" for i in range(1, 21)]
    rest = [s for s in seats if not s.startswith(("A", "B", "C", "D", "G"))]
    random.seed(RUN)
    plan = []
    dup_keys = [uuid.uuid4().hex for _ in range(300)]
    already = sum(tally[k] for k in tally if k in ("confirmed", "idempotent-replay") or k.startswith("declined") or k == "5xx")
    for _ in range(max(0, TOTAL - already)):
        u = random.randrange(100, USERS)
        seat_list = [random.choice(hot)] if random.random() < 0.6 else [random.choice(rest)]
        if random.random() < 0.15:
            seat_list = sorted({seat_list[0], random.choice(hot)})
        key = random.choice(dup_keys) if random.random() < 0.1 else None
        if key:  # duplicate keys must be reused with the same body+user, so derive deterministically
            u = int(key[:6], 16) % (USERS - 100) + 100
            seat_list = [hot[int(key[6:8], 16) % len(hot)]]
        plan.append((u, seat_list, key))
    t1 = time.time()
    await asyncio.gather(*[c.reserve(show, tokens[u], sl, key=k) for u, sl, k in plan])
    print(f"  {len(plan)} requests in {time.time() - t1:.1f}s ({len(plan) / max(time.time() - t1, 0.001):.0f} req/s)")

    print("\n=== Burst Results ===")
    for k in ("confirmed", "idempotent-replay", "cancelled"):
        print(f"- {k}: {tally[k]}")
    for k in sorted(tally):
        if k.startswith("declined:"):
            print(f"- Declined ({k.split(':', 1)[1]}): {tally[k]}")
    print(f"- 5xx errors: {tally['5xx']}   rate-limited(429): {tally['rate-limited']}   client transport errors: {tally['client-error']} {dict(client_errors)}")

    m_end = await metric_values(c)
    print("\n=== Reconciliation ===")
    st = (await c.call("GET", f"/shows/{show}?include_seats=false")).json()
    total = st["available"] + st["held"] + st["confirmed"]
    print(f"- Available: {st['available']}\n- Held: {st['held']}\n- Confirmed: {st['confirmed']}\n- Total: {st['total']}")
    check("available + held + confirmed == total_seats", total == st["total"] == SEATS, f"({total} vs {st['total']})")
    check("zero 5xx in whole burst (client-observed)", tally["5xx"] == 0)
    check("zero client transport errors (connection dropped / timeout)", tally["client-error"] == 0, f"({dict(client_errors)})")
    check("server-side http_5xx_total delta == 0", m_end.get("http_5xx_total", 0) - m0.get("http_5xx_total", 0) == 0)
    check("no seat confirmed twice (client view)", len(confirmed_codes) == len(set(confirmed_codes)))
    expected_confirmed = len(confirmed_codes)
    check("DB confirmed == non-replay 201 seats minus cancelled", st["confirmed"] == expected_confirmed, f"({st['confirmed']} vs {expected_confirmed})")
    m1 = m_end
    d_conf = m1.get("reservations_confirmed_total", 0) - m0.get("reservations_confirmed_total", 0)
    check("metrics: confirmed counter delta == confirmed 201s", d_conf == tally["confirmed"], f"({d_conf} vs {tally['confirmed']})")
    check("metrics: race_conditions_detected == 0", m1.get("race_conditions_detected_total", 0) == 0)
    gauge = m1.get(f'seats_confirmed{{show_id="{show}"}}')
    check("metrics: seats_confirmed gauge == API confirmed", gauge == st["confirmed"], f"({gauge} vs {st['confirmed']})")
    print(f"\nTotal wall time {time.time() - t_start:.1f}s")
    failed = [n for n, ok, _ in checks if not ok]
    print("RESULT:", "ALL CHECKS PASSED" if not failed else f"FAILED: {failed}")
    return 0 if not failed else 1


async def metric_values(c: Client) -> dict:
    r = await c.call("GET", "/metrics")
    out = {}
    for line in r.text.splitlines():
        if line and not line.startswith("#"):
            name, _, val = line.rpartition(" ")
            try:
                out[name] = float(val) if "." in val else int(val)
            except ValueError:
                pass
    return out


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
