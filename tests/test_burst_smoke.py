"""Runs the burst script (small) against a live server. Start the API first:  BASE_URL=http://localhost:8000 pytest -q"""
import os
import subprocess
import sys

import pytest

BASE = os.getenv("BASE_URL", "http://localhost:8000")


def test_burst_small_all_checks_pass():
    env = {**os.environ, "USERS": "80", "TOTAL": "800", "HOT_REQUESTS": "80", "SEATS": "500"}
    p = subprocess.run([sys.executable, "scripts/burst.py", BASE], env=env, capture_output=True, text=True, timeout=300)
    print(p.stdout[-3000:])
    if "admin login failed" in p.stdout:
        pytest.skip("server not reachable")
    assert p.returncode == 0, p.stdout[-3000:]
