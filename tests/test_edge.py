"""Envoy edge tests.

Skipped unless EDGE_URL is set, because these need the compose stack rather than
Testcontainers — the point is the proxy in front, not the app. CI runs them in the
`stack` job after `docker compose up`.

    EDGE_URL=http://localhost:8080 EDGE_TLS_URL=https://localhost:8443 pytest tests/test_edge.py
"""

import asyncio
import os
import time
from collections import Counter

import httpx
import pytest

EDGE_URL = os.environ.get("EDGE_URL")
EDGE_TLS_URL = os.environ.get("EDGE_TLS_URL")

pytestmark = pytest.mark.skipif(not EDGE_URL, reason="EDGE_URL not set; compose stack not running")

# Must match the token_bucket in envoy/envoy.yaml.
LIMIT_PER_S = 100
REQUESTS = int(os.environ.get("EDGE_REQUESTS", 600))
CONNS = int(os.environ.get("EDGE_CONNS", 300))


async def test_tls_termination_serves_the_gateway():
    if not EDGE_TLS_URL:
        pytest.skip("EDGE_TLS_URL not set")
    # verify=False: the cert is self-signed by the certgen service on every `up`.
    async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
        r = await client.get(f"{EDGE_TLS_URL}/health")

    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_a_real_completion_routes_through_the_edge():
    """Proves the proxy path carries POST bodies to the gateway, not just /health."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"{EDGE_URL}/v1/chat",
            json={
                "messages": [{"role": "user", "content": "through the edge"}],
                "policy": "cheapest",
                "temperature": 0.9,
            },
        )

    assert r.status_code == 200, r.text
    assert r.json()["content"]


async def test_edge_rate_limit_sheds_the_excess():
    """Fired concurrently: the bucket refills 100 tokens a second, so a sequential
    loop never gets ahead of it.

    If the client cannot push faster than the configured limit, this skips rather
    than fails — on Docker Desktop for Windows the host port-forward tops out
    around 35 req/s, which says nothing about the limiter. On Linux (and in CI)
    the loopback is fast enough for the assertion to mean something.
    """
    async with httpx.AsyncClient(
        timeout=30.0, limits=httpx.Limits(max_connections=CONNS)
    ) as client:
        started = time.perf_counter()
        responses = await asyncio.gather(
            *(client.get(f"{EDGE_URL}/health") for _ in range(REQUESTS))
        )
        elapsed = time.perf_counter() - started

    codes = Counter(r.status_code for r in responses)
    limited = [r for r in responses if r.status_code == 429]
    achieved_rate = REQUESTS / elapsed

    if not limited and achieved_rate < LIMIT_PER_S:
        pytest.skip(
            f"client only achieved {achieved_rate:.0f} req/s against a {LIMIT_PER_S} req/s "
            f"limit — the harness is the bottleneck, not the limiter"
        )

    assert codes[200] > 0, "everything was rejected; the limit is too tight to be useful"
    assert limited, f"nothing shed at {achieved_rate:.0f} req/s over a {LIMIT_PER_S} req/s limit"
    # The header comes from response_headers_to_add on the local_ratelimit filter;
    # x-envoy-ratelimited is set by the *global* limiter and is not present here.
    assert all(r.headers.get("x-rate-limited-by") == "envoy-edge" for r in limited)
