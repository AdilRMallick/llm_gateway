"""Artifact 3 — what routing policy costs.

Runs the identical workload under each policy and reports actual dollars from the
per-request accounting, not a rate-card estimate.

Method
------
* One seeded workload of `PROMPTS` prompts, replayed unchanged for every policy,
  so the only variable is the routing decision.
* temperature=0.7 for the policy rows, which puts them above
  `cache_max_temperature` and guarantees every request reaches a provider. A
  cheaper policy must win on routing, not on getting luckier with the cache.
* One extra row runs cheapest-first at temperature=0.0 with the cache on, to show
  what the two mechanisms are worth together.
* Token counts come from the provider's usage block; cost is tokens × the rate
  card in app/pricing.py, which /stats stamps with its as-of date.

Run: python -m bench.cost_compare
"""

import asyncio
import os
import random
import uuid

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

PROMPTS = int(os.environ.get("BENCH_COST_PROMPTS", 60))
SEED = int(os.environ.get("BENCH_SEED", 42))
MOCK_LATENCY_MS = float(os.environ.get("BENCH_MOCK_LATENCY_MS", 20))

SCENARIOS = [
    ("pinned: anthropic", {"policy": "pinned", "provider": "anthropic"}, 0.7),
    ("pinned: openai", {"policy": "pinned", "provider": "openai"}, 0.7),
    ("pinned: google", {"policy": "pinned", "provider": "google"}, 0.7),
    ("fastest", {"policy": "fastest"}, 0.7),
    ("cheapest", {"policy": "cheapest"}, 0.7),
    ("cheapest + cache", {"policy": "cheapest"}, 0.0),
]

TOPICS = [
    "vector index rebuild strategy",
    "p99 latency regression after a deploy",
    "hot partition in a sharded write path",
    "backfill without blocking the primary",
    "cache stampede on a popular key",
    "retry storm caused by a client library default",
]


def workload(nonce: str) -> list[str]:
    """PROMPTS requests drawn from PROMPTS/2 distinct prompts, deterministically
    shuffled — a 50% repeat rate.

    Real traffic repeats itself; a workload of all-unique prompts would make the
    cache look worthless and a workload of one prompt would make it look magic.
    Every scenario gets this same sequence, so the cache row is comparable to the
    uncached rows request-for-request.
    """
    rng = random.Random(SEED)
    distinct = [
        f"[{nonce}] Explain {rng.choice(TOPICS)} in {rng.randint(2, 4)} sentences. (item {i})"
        for i in range(PROMPTS // 2)
    ]
    sequence = distinct * 2
    rng.shuffle(sequence)
    return sequence


async def run_scenario(
    client: httpx.AsyncClient, name: str, policy: dict, temperature: float, prompts: list[str]
) -> dict:
    cost = 0.0
    tokens_in = tokens_out = 0
    hits = 0
    served: dict[str, int] = {}

    for p in prompts:
        status, payload, _ = await chat(
            client,
            {
                "messages": [{"role": "user", "content": p}],
                "temperature": temperature,
                "max_tokens": 256,
                **policy,
            },
        )
        assert status == 200, payload
        cost += payload["usage"]["cost_usd"]
        tokens_in += payload["usage"]["tokens_in"]
        tokens_out += payload["usage"]["tokens_out"]
        hits += int(payload["cache_hit"])
        served[payload["provider"]] = served.get(payload["provider"], 0) + 1

    return {
        "scenario": name,
        "requests": len(prompts),
        "served_by": served,
        "cache_hits": hits,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": round(cost, 6),
        "cost_per_1k_requests_usd": round(cost / len(prompts) * 1000, 4),
    }


async def main() -> None:
    nonce = uuid.uuid4().hex[:8]
    results = []

    async with httpx.AsyncClient(timeout=30.0) as client:
        await wait_for_gateway(client)
        await reset_mock(client)
        for p in ("anthropic", "openai", "google"):
            await set_mock(client, p, latency_ms=MOCK_LATENCY_MS, latency_jitter_ms=0.0)

        for name, policy, temperature in SCENARIOS:
            # A fresh nonce per scenario keeps the cache from leaking across rows;
            # the prompt text itself is seeded, so the workload is the same shape.
            prompts = workload(f"{nonce}-{name.replace(' ', '')}")
            rule(f"scenario: {name}  (temperature={temperature})")
            row = await run_scenario(client, name, policy, temperature, prompts)
            print(
                f"  served_by={row['served_by']}  cache_hits={row['cache_hits']}  "
                f"cost=${row['cost_usd']:.6f}"
            )
            results.append(row)

        stats = (await client.get(f"{GATEWAY_URL}/stats", params={"window_minutes": 15})).json()

    baseline = max(r["cost_usd"] for r in results)
    for r in results:
        if not baseline or r["cost_usd"] == baseline:
            r["vs_most_expensive"] = "baseline"
        else:
            r["vs_most_expensive"] = f"-{round((1 - r['cost_usd'] / baseline) * 100, 1)}%"
        r["served_by"] = ", ".join(f"{k}x{v}" for k, v in r["served_by"].items())

    rule(f"cost for the same {PROMPTS}-request workload")
    table(
        results,
        [
            "scenario",
            "served_by",
            "cache_hits",
            "tokens_in",
            "tokens_out",
            "cost_usd",
            "cost_per_1k_requests_usd",
            "vs_most_expensive",
        ],
    )

    cheapest = next(r for r in results if r["scenario"] == "cheapest")
    pinned_worst = max(
        (r for r in results if r["scenario"].startswith("pinned")), key=lambda r: r["cost_usd"]
    )
    saving = round((1 - cheapest["cost_usd"] / pinned_worst["cost_usd"]) * 100, 1)
    print(
        f"\ncheapest-first vs {pinned_worst['scenario']}: "
        f"${pinned_worst['cost_usd']:.6f} → ${cheapest['cost_usd']:.6f}  ({saving}% lower)"
    )
    print(f"rate card as of: {stats['pricing_as_of']}")

    path = save(
        "cost_compare",
        {
            "scenarios": results,
            "cheapest_vs_worst_pinned_pct": saving,
            "pricing_as_of": stats["pricing_as_of"],
        },
        Method(
            runs=PROMPTS * len(SCENARIOS),
            warmups_discarded=0,
            concurrency=1,
            prompts=PROMPTS,
            mock_latency_ms=MOCK_LATENCY_MS,
            notes=(
                f"Seeded workload (seed={SEED}) replayed per scenario. Costs are the sum of "
                "per-request accounting rows, priced from app/pricing.py."
            ),
        ),
    )
    print(f"wrote {path}")


if __name__ == "__main__":
    asyncio.run(main())
