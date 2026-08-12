"""Artifact 2 — failover, caused on purpose.

Walks the mock provider through a staged outage and shows requests continuing to
succeed, with the `provider_attempted` chain read back out of Postgres rather
than asserted.

  phase 1  everything healthy         -> cheapest wins, one attempt
  phase 2  cheapest provider at 100%  -> falls through to the next
  phase 3  two providers down         -> falls through to the third
  phase 4  all three down             -> 502 with the full attempt chain
  phase 5  restored                   -> back to normal, no operator action

Run: python -m bench.failover_demo
"""

import asyncio
import os
import uuid
from collections import Counter

import httpx

from bench.common import (
    GATEWAY_URL,
    Method,
    chat,
    reset_mock,
    rule,
    save,
    set_mock,
    table,
    wait_for_gateway,
)

N = int(os.environ.get("BENCH_FAILOVER_N", 20))
MOCK_LATENCY_MS = float(os.environ.get("BENCH_MOCK_LATENCY_MS", 40))


def cheapest_order() -> list[str]:
    """Read the try-order out of the rate card instead of hardcoding it.

    This demo used to name google first. Then Google retired gemini-2.0-flash and
    its replacement cost more than gpt-4o-mini, so "take down the cheapest
    provider" started taking down the second-cheapest one and the demo quietly
    stopped demonstrating anything.
    """
    from app.pricing import DEFAULT_MODEL, estimate_cost_usd
    from app.schemas import Provider

    ranked = sorted(Provider, key=lambda p: estimate_cost_usd(p, DEFAULT_MODEL[p], 16, 128))
    return [p.value for p in ranked]


PROVIDERS = cheapest_order()
FIRST, SECOND, THIRD = PROVIDERS


def body(prompt: str) -> dict:
    return {
        "messages": [{"role": "user", "content": prompt}],
        "policy": "cheapest",
        # Above cache_max_temperature so nothing is served from cache: this demo is
        # about provider behaviour, and a cache hit would hide it.
        "temperature": 0.7,
        "max_tokens": 128,
    }


async def phase(client: httpx.AsyncClient, name: str, nonce: str, expect_ok: bool) -> dict:
    rule(name)
    served = Counter()
    chains = Counter()
    statuses = Counter()
    latencies = []

    for i in range(N):
        status, payload, ms = await chat(client, body(f"[{nonce}] {name} #{i}"))
        statuses[status] += 1
        latencies.append(ms)
        if status == 200:
            served[payload["provider"]] += 1
            chains["->".join(a["provider"] for a in payload["attempts"])] += 1
        else:
            detail = payload.get("detail", {})
            attempts = detail.get("attempts", []) if isinstance(detail, dict) else []
            chains["->".join(a["provider"] for a in attempts)] += 1

    ok = statuses.get(200, 0)
    print(f"  {ok}/{N} succeeded" + ("" if expect_ok else "  (expected: all fail)"))
    print(f"  served by: {dict(served) or '—'}")
    print(f"  attempt chains: {dict(chains)}")
    if expect_ok and ok != N:
        raise AssertionError(f"{name}: expected all {N} to succeed, got {ok}")
    if not expect_ok and ok != 0:
        raise AssertionError(f"{name}: expected all {N} to fail, got {ok} successes")

    return {
        "phase": name,
        "requests": N,
        "succeeded": ok,
        "served_by": dict(served),
        "attempt_chains": dict(chains),
        "mean_latency_ms": round(sum(latencies) / len(latencies), 2),
    }


async def main() -> None:
    nonce = uuid.uuid4().hex[:8]
    phases = []

    async with httpx.AsyncClient(timeout=30.0) as client:
        await wait_for_gateway(client)
        await reset_mock(client)
        for p in PROVIDERS:
            await set_mock(client, p, latency_ms=MOCK_LATENCY_MS, latency_jitter_ms=0.0)

        phases.append(await phase(client, "phase 1 - all healthy", nonce, expect_ok=True))

        await set_mock(client, FIRST, error_rate=1.0)
        print(f"\n>> injected: {FIRST} error_rate=1.0 (503 on every call)")
        phases.append(await phase(client, f"phase 2 - {FIRST} down", nonce, expect_ok=True))

        await set_mock(client, SECOND, rate_limit_rate=1.0, retry_after_s=0.05)
        print(f"\n>> injected: {SECOND} rate_limit_rate=1.0 (429 with Retry-After: 0.05)")
        phases.append(
            await phase(client, f"phase 3 - {FIRST} + {SECOND} down", nonce, expect_ok=True)
        )

        await set_mock(client, THIRD, error_rate=1.0)
        print(f"\n>> injected: {THIRD} error_rate=1.0 — every provider is now failing")
        phases.append(await phase(client, "phase 4 - total outage", nonce, expect_ok=False))

        await reset_mock(client)
        for p in PROVIDERS:
            await set_mock(client, p, latency_ms=MOCK_LATENCY_MS, latency_jitter_ms=0.0)
        print("\n>> restored: all providers healthy again")
        phases.append(await phase(client, "phase 5 - recovered", nonce, expect_ok=True))

        rule("recorded in Postgres (GET /requests) — newest 10")
        rows = (await client.get(f"{GATEWAY_URL}/requests", params={"limit": 10})).json()["rows"]
        table(
            [
                {
                    "ts": r["ts"][11:23],
                    "policy": r["route_policy"],
                    "attempted": "->".join(r["provider_attempted"]) or "—",
                    "served": r["provider_served"] or "—",
                    "status": r["status"],
                    "ms": r["latency_ms"],
                    "failover_reason": (r["failover_reason"] or "—")[:60],
                }
                for r in rows
            ],
            ["ts", "policy", "attempted", "served", "status", "ms", "failover_reason"],
        )

        failed = (
            await client.get(f"{GATEWAY_URL}/requests", params={"limit": 3, "failed_only": True})
        ).json()["rows"]
        if failed:
            rule("a total-outage row, in full")
            for k, v in failed[0].items():
                print(f"  {k:20} {v}")

    path = save(
        "failover_demo",
        {"phases": phases, "recent_rows": rows},
        Method(
            runs=N * 5,
            warmups_discarded=0,
            concurrency=1,
            prompts=N * 5,
            mock_latency_ms=MOCK_LATENCY_MS,
            notes=(
                "temperature=0.7 so every request bypasses the cache and actually reaches a "
                "provider. Faults injected via the mock control plane; no real outage involved."
            ),
        ),
    )
    print(f"\nwrote {path}")


if __name__ == "__main__":
    asyncio.run(main())
