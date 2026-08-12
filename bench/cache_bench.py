"""Artifact 1 — cold vs warm latency.

Method
------
* `PROMPTS` distinct prompts, each tagged with a per-run nonce so every cold call
  is a genuine cache miss without needing to flush Redis (which would make the
  benchmark depend on Redis being reachable from the host).
* Warmup pass first, discarded: it pays for HTTP connection setup, the asyncpg
  pool, and the first-call import cost, none of which is what we are measuring.
* Cold pass: each prompt once. Every one misses.
* Warm pass: the same prompts, `REPEATS` times. Every one hits.
* Latency is measured client-side, so it includes HTTP and framework overhead.
  The gateway's own view from /stats is printed next to it as a cross-check; the
  gap between the two is what the transport costs.
* The mock provider is pinned to a fixed latency with no jitter, so the cold
  number is dominated by a known, reproducible provider cost rather than by
  whatever the network was doing.

Run: python -m bench.cache_bench
"""

import asyncio
import os
import uuid

import httpx

from bench.common import (
    GATEWAY_URL,
    Method,
    chat,
    percentiles,
    preexisting_rows,
    reset_mock,
    rule,
    save,
    set_mock,
    table,
    wait_for_gateway,
)

PROMPTS = int(os.environ.get("BENCH_PROMPTS", 50))
REPEATS = int(os.environ.get("BENCH_REPEATS", 4))
WARMUPS = int(os.environ.get("BENCH_WARMUPS", 10))
MOCK_LATENCY_MS = float(os.environ.get("BENCH_MOCK_LATENCY_MS", 120))


def body(prompt: str) -> dict:
    return {
        "messages": [{"role": "user", "content": prompt}],
        "policy": "cheapest",
        "temperature": 0.0,
        "max_tokens": 256,
    }


async def main() -> None:
    nonce = uuid.uuid4().hex[:8]
    prompts = [f"[{nonce}] Summarize topic number {i} in one sentence." for i in range(PROMPTS)]

    async with httpx.AsyncClient(timeout=30.0) as client:
        await wait_for_gateway(client)
        await reset_mock(client)
        # Fixed provider latency, no jitter: the cold number should be reproducible.
        for p in ("anthropic", "openai", "google"):
            await set_mock(client, p, latency_ms=MOCK_LATENCY_MS, latency_jitter_ms=0.0)

        rows_before = await preexisting_rows(client)
        if rows_before > 0:
            print(
                f"\nNOTE: {rows_before} requests already on record. This is a warm stack;\n"
                f"      the committed numbers come from `docker compose down -v && up -d`\n"
                f"      with this benchmark run first. Expect a slower warm p50 here."
            )

        rule(f"warmup ({WARMUPS} requests, discarded)")
        for i in range(WARMUPS):
            await chat(client, body(f"[{nonce}] warmup {i}"))

        rule(f"cold pass — {PROMPTS} distinct prompts, all misses")
        cold: list[float] = []
        for p in prompts:
            status, payload, ms = await chat(client, body(p))
            assert status == 200, payload
            assert payload["cache_hit"] is False, f"expected a miss: {payload}"
            cold.append(ms)

        rule(f"warm pass — same {PROMPTS} prompts × {REPEATS}, all hits")
        warm: list[float] = []
        for _ in range(REPEATS):
            for p in prompts:
                status, payload, ms = await chat(client, body(p))
                assert status == 200, payload
                assert payload["cache_hit"] is True, f"expected a hit: {payload}"
                warm.append(ms)

        stats = (await client.get(f"{GATEWAY_URL}/stats", params={"window_minutes": 15})).json()

    cold_p, warm_p = percentiles(cold), percentiles(warm)
    rule("client-observed latency (ms)")
    table(
        [
            {"pass": "cold (miss)", **{k: v for k, v in cold_p.items()}},
            {"pass": "warm (hit)", **{k: v for k, v in warm_p.items()}},
        ],
        ["pass", "n", "p50", "p95", "p99", "mean", "min", "max"],
    )

    speedup_p50 = round(cold_p["p50"] / warm_p["p50"], 1) if warm_p["p50"] else None
    speedup_p95 = round(cold_p["p95"] / warm_p["p95"], 1) if warm_p["p95"] else None
    print(f"\np50: {cold_p['p50']} ms → {warm_p['p50']} ms  ({speedup_p50}× faster)")
    print(f"p95: {cold_p['p95']} ms → {warm_p['p95']} ms  ({speedup_p95}× faster)")

    rule("server-side cross-check (/stats latency_by_cache)")
    table(
        [
            {
                "cache_hit": r["cache_hit"],
                "requests": r["requests"],
                "p50_ms": r["p50_ms"],
                "p95_ms": r["p95_ms"],
            }
            for r in stats["latency_by_cache"]
        ],
        ["cache_hit", "requests", "p50_ms", "p95_ms"],
    )

    path = save(
        "cache_bench",
        {
            "cold_ms": cold_p,
            "warm_ms": warm_p,
            "speedup_p50": speedup_p50,
            "speedup_p95": speedup_p95,
            "server_side": stats["latency_by_cache"],
        },
        Method(
            runs=len(cold) + len(warm),
            warmups_discarded=WARMUPS,
            concurrency=1,
            prompts=PROMPTS,
            mock_latency_ms=MOCK_LATENCY_MS,
            preexisting_rows=rows_before,
            notes=(
                "Sequential client. Cold = first call per prompt; warm = same prompts replayed. "
                "Accounting rows are written off the request path, so these times are the "
                "served path only."
            ),
        ),
    )
    print(f"\nwrote {path}")


if __name__ == "__main__":
    asyncio.run(main())
