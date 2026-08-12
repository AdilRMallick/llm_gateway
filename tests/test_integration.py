"""End-to-end: real Postgres, real Redis, real provider schemas.

Everything the README claims is asserted here, so the claims fail in CI rather
than in an interview.
"""

import asyncio
import uuid

import pytest

from tests.conftest import chat_body, cheapest_order

pytestmark = pytest.mark.usefixtures("clean_cache")


def uniq(label: str) -> str:
    return f"{label}-{uuid.uuid4().hex[:8]}"


async def counts(mock_client, provider: str) -> dict:
    return (await mock_client.get("/_control")).json()[provider]["counts"]


# Derived from the rate card, not hardcoded: see cheapest_order() in conftest.
FIRST, SECOND, THIRD = cheapest_order()


# --- normalization ---------------------------------------------------------


@pytest.mark.parametrize("provider", ["anthropic", "openai", "google"])
async def test_same_request_same_response_shape_from_every_provider(app_client, provider):
    r = await app_client.post(
        "/v1/chat", json=chat_body(uniq("normalize"), policy="pinned", provider=provider)
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"] == provider
    assert body["content"]
    assert body["usage"]["tokens_in"] > 0
    assert body["usage"]["tokens_out"] > 0
    assert body["usage"]["cost_usd"] > 0
    assert set(body) == {
        "id",
        "content",
        "provider",
        "model",
        "cache_hit",
        "latency_ms",
        "usage",
        "attempts",
        "policy",
    }


async def test_cheapest_policy_picks_the_cheapest_provider(app_client):
    r = await app_client.post("/v1/chat", json=chat_body(uniq("cheap")))

    assert r.json()["provider"] == FIRST


# --- caching ---------------------------------------------------------------


async def test_second_identical_request_is_a_cache_hit(app_client, mock_client):
    body = chat_body(uniq("cache"))

    first = (await app_client.post("/v1/chat", json=body)).json()
    second = (await app_client.post("/v1/chat", json=body)).json()

    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert second["content"] == first["content"]
    assert second["usage"]["cost_usd"] == 0.0  # nothing was billed
    assert (await counts(mock_client, FIRST))["calls"] == 1


async def test_requests_differing_only_in_temperature_do_not_share_a_cache_entry(app_client):
    prompt = uniq("temp")
    await app_client.post("/v1/chat", json=chat_body(prompt, temperature=0.0))
    second = await app_client.post("/v1/chat", json=chat_body(prompt, temperature=0.5))

    assert second.json()["cache_hit"] is False


async def test_sampled_requests_are_not_cached_at_all(app_client, mock_client):
    """Above cache_max_temperature the caller asked to sample; replaying one
    recorded sample forever would be the wrong semantics."""
    body = chat_body(uniq("sampled"), temperature=0.9)

    for _ in range(3):
        assert (await app_client.post("/v1/chat", json=body)).json()["cache_hit"] is False

    assert (await counts(mock_client, FIRST))["calls"] == 3


async def test_concurrent_misses_for_the_same_prompt_make_one_provider_call(
    app_client, mock_client
):
    """The thundering-herd case: ten identical requests arrive with a cold cache."""
    await mock_client.post(f"/_control/{FIRST}", json={"latency_ms": 150.0})
    body = chat_body(uniq("stampede"))

    results = await asyncio.gather(*(app_client.post("/v1/chat", json=body) for _ in range(10)))
    payloads = [r.json() for r in results]

    assert all(r.status_code == 200 for r in results)
    assert (await counts(mock_client, FIRST))["calls"] == 1, "single-flight did not hold"
    assert sum(1 for p in payloads if p["cache_hit"]) == 9
    assert len({p["content"] for p in payloads}) == 1


async def test_redis_outage_degrades_to_no_caching_and_nothing_else(app_client):
    import redis.asyncio as aioredis

    working = app_client.app.state.cache.redis
    app_client.app.state.cache.redis = aioredis.from_url(
        "redis://127.0.0.1:1", socket_connect_timeout=0.05
    )
    try:
        body = chat_body(uniq("noredis"))
        first = await app_client.post("/v1/chat", json=body)
        second = await app_client.post("/v1/chat", json=body)

        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["cache_hit"] is False  # no cache, but still served
    finally:
        app_client.app.state.cache.redis = working


# --- failover --------------------------------------------------------------


async def test_failover_moves_to_the_next_provider(app_client, mock_client):
    await mock_client.post(f"/_control/{FIRST}", json={"error_rate": 1.0})

    body = (await app_client.post("/v1/chat", json=chat_body(uniq("failover")))).json()

    assert body["provider"] == SECOND
    assert [a["provider"] for a in body["attempts"]] == [FIRST, FIRST, SECOND]
    assert [a["status"] for a in body["attempts"]] == ["error", "error", "ok"]
    assert all(a["error_kind"] == "server_error" for a in body["attempts"][:2])


async def test_rate_limit_is_retried_then_failed_over(app_client, mock_client):
    await mock_client.post(
        f"/_control/{FIRST}", json={"rate_limit_rate": 1.0, "retry_after_s": 0.01}
    )

    body = (await app_client.post("/v1/chat", json=chat_body(uniq("429")))).json()

    assert body["provider"] == SECOND
    assert [a["error_kind"] for a in body["attempts"] if a["status"] == "error"] == [
        "rate_limited",
        "rate_limited",
    ]


async def test_a_400_is_not_retried_on_the_same_provider(app_client, mock_client):
    """Retrying a malformed request just burns latency: it will stay malformed."""
    await mock_client.post(f"/_control/{FIRST}", json={"bad_request_rate": 1.0})

    body = (await app_client.post("/v1/chat", json=chat_body(uniq("400")))).json()

    assert body["provider"] == SECOND
    assert [a["provider"] for a in body["attempts"]] == [FIRST, SECOND]
    assert (await counts(mock_client, FIRST))["calls"] == 1


async def test_total_outage_returns_502_with_the_full_attempt_chain(app_client, mock_client):
    for p in ("anthropic", "openai", "google"):
        await mock_client.post(f"/_control/{p}", json={"error_rate": 1.0})

    r = await app_client.post("/v1/chat", json=chat_body(uniq("outage")))

    assert r.status_code == 502
    detail = r.json()["detail"]
    assert detail["error"] == "all_providers_failed"
    assert [a["provider"] for a in detail["attempts"]] == [
        FIRST,
        FIRST,
        SECOND,
        SECOND,
        THIRD,
        THIRD,
    ]


async def test_a_pin_never_falls_through(app_client, mock_client):
    await mock_client.post("/_control/google", json={"error_rate": 1.0})

    r = await app_client.post(
        "/v1/chat", json=chat_body(uniq("pinned"), policy="pinned", provider="google")
    )

    assert r.status_code == 502
    assert {a["provider"] for a in r.json()["detail"]["attempts"]} == {"google"}
    assert (await counts(mock_client, "openai"))["calls"] == 0


async def test_pinned_without_provider_is_400(app_client):
    r = await app_client.post("/v1/chat", json=chat_body(uniq("nopin"), policy="pinned"))

    assert r.status_code == 400


async def test_recovery_needs_no_operator_action(app_client, mock_client):
    await mock_client.post(f"/_control/{FIRST}", json={"error_rate": 1.0})
    assert (await app_client.post("/v1/chat", json=chat_body(uniq("down")))).json()[
        "provider"
    ] == SECOND

    await mock_client.post(f"/_control/{FIRST}", json={"error_rate": 0.0})
    assert (await app_client.post("/v1/chat", json=chat_body(uniq("up")))).json()[
        "provider"
    ] == FIRST


# --- accounting ------------------------------------------------------------


async def test_the_failover_chain_is_recorded_in_postgres(app_client, mock_client, clean_db):
    await mock_client.post(f"/_control/{FIRST}", json={"error_rate": 1.0})
    await app_client.post("/v1/chat", json=chat_body(uniq("recorded")))

    row = (await app_client.get("/requests", params={"limit": 1})).json()["rows"][0]

    assert row["provider_attempted"] == [FIRST, FIRST, SECOND]
    assert row["provider_served"] == SECOND
    assert row["status"] == "ok"
    assert f"{FIRST}:server_error/503" in row["failover_reason"]
    assert row["cost_usd"] > 0


async def test_a_failed_request_is_recorded_too(app_client, mock_client, clean_db):
    for p in ("anthropic", "openai", "google"):
        await mock_client.post(f"/_control/{p}", json={"error_rate": 1.0})
    await app_client.post("/v1/chat", json=chat_body(uniq("failrow")))

    row = (await app_client.get("/requests", params={"limit": 1, "failed_only": True})).json()[
        "rows"
    ][0]

    assert row["status"] == "error"
    assert row["provider_served"] is None
    assert row["cost_usd"] == 0.0
    assert len(row["provider_attempted"]) == 6


async def test_usage_is_estimated_and_flagged_when_a_provider_omits_it(
    app_client, mock_client, clean_db
):
    await mock_client.post(f"/_control/{FIRST}", json={"usage_reported": False})

    body = (await app_client.post("/v1/chat", json=chat_body(uniq("nousage")))).json()
    row = (await app_client.get("/requests", params={"limit": 1})).json()["rows"][0]

    assert body["usage"]["tokens_in"] > 0  # estimated, not silently zero
    assert body["usage"]["cost_usd"] > 0
    assert row["usage_estimated"] is True


async def test_stats_aggregate_matches_what_was_served(app_client, clean_db):
    body = chat_body(uniq("stats"))
    await app_client.post("/v1/chat", json=body)  # miss
    await app_client.post("/v1/chat", json=body)  # hit
    await app_client.post("/v1/chat", json=chat_body(uniq("stats2"), policy="fastest"))

    stats = (await app_client.get("/stats", params={"window_minutes": 5})).json()

    assert stats["totals"]["requests"] == 3
    assert stats["totals"]["cache_hits"] == 1
    assert stats["totals"]["cache_hit_rate"] == pytest.approx(1 / 3, abs=0.01)
    assert stats["totals"]["errors"] == 0
    assert stats["totals"]["cost_usd"] > 0
    assert {p["policy"] for p in stats["by_policy"]} == {"cheapest", "fastest"}
    assert stats["accounting"]["dropped"] == 0
    assert stats["pricing_as_of"]


async def test_cache_hits_are_faster_than_misses_server_side(app_client, mock_client, clean_db):
    """The cache benchmark's claim, at test scale."""
    await mock_client.post(f"/_control/{FIRST}", json={"latency_ms": 100.0})
    body = chat_body(uniq("latency"))

    miss = (await app_client.post("/v1/chat", json=body)).json()
    hit = (await app_client.post("/v1/chat", json=body)).json()

    assert miss["latency_ms"] >= 100
    assert hit["latency_ms"] < miss["latency_ms"] / 5


async def test_cheapest_costs_less_than_pinning_the_expensive_provider(app_client, clean_db):
    prompt = uniq("cost")
    cheap = (await app_client.post("/v1/chat", json=chat_body(prompt + "-a"))).json()
    pricey = (
        await app_client.post(
            "/v1/chat", json=chat_body(prompt + "-b", policy="pinned", provider="anthropic")
        )
    ).json()

    assert cheap["usage"]["cost_usd"] < pricey["usage"]["cost_usd"]


# --- health endpoints ------------------------------------------------------


async def test_health_and_readiness(app_client):
    assert (await app_client.get("/health")).json() == {"status": "ok"}

    ready = (await app_client.get("/ready")).json()
    assert ready["status"] == "ready"
    assert ready["postgres"] == "up"
    assert ready["redis"] == "up"


async def test_health_tracker_records_observed_latency(app_client, mock_client):
    await mock_client.post(f"/_control/{FIRST}", json={"latency_ms": 60.0})
    await app_client.post("/v1/chat", json=chat_body(uniq("health"), temperature=0.9))

    stats = (await app_client.get("/stats", params={"window_minutes": 5})).json()
    served = next(h for h in stats["health"] if h["provider"] == FIRST)

    assert served["samples"] >= 1
    assert served["p50_ms"] >= 60
    assert served["error_rate"] == 0.0
