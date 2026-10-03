#!/bin/sh
# usage: ./burst.sh <BASE_URL>      (env knobs: USERS TOTAL SEATS HOT_REQUESTS CONCURRENCY ADMIN_EMAIL ADMIN_PASSWORD)
exec python scripts/burst.py "${1:-http://localhost:8000}"
