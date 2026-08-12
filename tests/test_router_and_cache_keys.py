"""Routing order and cache-key semantics. Pure logic, no containers."""

import pytest

from app.cache import cache_key
from app.config import Settings
from app.health import ProviderHealth
from app.pricing import DEFAULT_MODEL, cost_usd, estimate_cost_usd
from app.router import Router
from app.schemas import ChatRequest, Message, Provider, RoutePolicy


def req(**overrides) -> ChatRequest:
    body = {"messages": [Message(role="user", content="hello")], "max_tokens": 256}
    body.update(overrides)
    return ChatRequest(**body)


def health_with(latencies: dict[Provider, list[tuple[float, bool]]]) -> ProviderHealth:
    h = ProviderHealth(None, Settings())  # None redis -> in-process window
    for provider, samples in latencies.items():
        for latency, ok in samples:
            h._local[provider].append((__import__("time").time(), latency, ok))
    return h


# --- routing ---------------------------------------------------------------


async def test_cheapest_orders_by_estimated_cost():
    """Asserts the ordering property, not today's price list — the rate card is
    data that moves, and a provider retiring a model should not fail this."""
    plan = await Router(health_with({})).plan(req(policy=RoutePolicy.cheapest))

    assert {c.provider for c in plan} == set(Provider)
    assert [c.est_cost_usd for c in plan] == sorted(c.est_cost_usd for c in plan)
    assert plan[0].est_cost_usd < plan[-1].est_cost_usd, "all providers priced identically"


async def test_fastest_orders_by_observed_p50():
    health = health_with(
        {
            Provider.anthropic: [(10.0, True)] * 5,
            Provider.openai: [(200.0, True)] * 5,
            Provider.google: [(80.0, True)] * 5,
        }
    )
    plan = await Router(health).plan(req(policy=RoutePolicy.fastest))

    assert [c.provider for c in plan] == [Provider.anthropic, Provider.google, Provider.openai]


async def test_fastest_penalises_a_fast_but_failing_provider():
    """Score is expected ms per *successful* answer, so 10ms at a 90% error rate
    (=100) loses to a reliable 80ms provider."""
    health = health_with(
        {
            Provider.anthropic: [(10.0, True)] + [(10.0, False)] * 9,
            Provider.google: [(80.0, True)] * 10,
        }
    )
    plan = await Router(health).plan(req(policy=RoutePolicy.fastest))

    order = [c.provider for c in plan]
    assert order.index(Provider.google) < order.index(Provider.anthropic)


async def test_a_moderately_unreliable_provider_can_still_be_the_fastest_choice():
    """The flip side, stated so the rule is not mistaken for 'errors disqualify':
    10ms at a 20% error rate (=12.5) still beats a reliable 80ms provider."""
    health = health_with(
        {
            Provider.anthropic: [(10.0, True)] * 8 + [(10.0, False)] * 2,
            Provider.google: [(80.0, True)] * 10,
        }
    )
    plan = await Router(health).plan(req(policy=RoutePolicy.fastest))

    order = [c.provider for c in plan]
    assert order.index(Provider.anthropic) < order.index(Provider.google)


async def test_unmeasured_provider_is_tried_first_under_fastest():
    """Zero samples scores 0, so a cold provider gets a chance to fill its window."""
    health = health_with({Provider.google: [(80.0, True)] * 10})
    plan = await Router(health).plan(req(policy=RoutePolicy.fastest))

    assert plan[0].provider is not Provider.google


async def test_pinned_returns_exactly_one_candidate():
    """A pin that falls through to another provider is not a pin."""
    plan = await Router(health_with({})).plan(
        req(policy=RoutePolicy.pinned, provider=Provider.anthropic)
    )

    assert [c.provider for c in plan] == [Provider.anthropic]


async def test_pinned_without_a_provider_is_a_client_error():
    with pytest.raises(ValueError, match="requires a provider"):
        await Router(health_with({})).plan(req(policy=RoutePolicy.pinned))


async def test_model_override_is_respected_and_repriced():
    plan = await Router(health_with({})).plan(
        req(policy=RoutePolicy.pinned, provider=Provider.openai, models={Provider.openai: "gpt-4o"})
    )

    assert plan[0].model == "gpt-4o"
    assert plan[0].est_cost_usd > estimate_cost_usd(Provider.openai, "gpt-4o-mini", 3, 256)


async def test_unknown_model_prices_at_zero_rather_than_crashing():
    plan = await Router(health_with({})).plan(
        req(
            policy=RoutePolicy.pinned,
            provider=Provider.openai,
            models={Provider.openai: "gpt-nonexistent"},
        )
    )

    assert plan[0].est_cost_usd == 0.0


def test_default_models_are_all_priced():
    for provider, model in DEFAULT_MODEL.items():
        assert cost_usd(provider, model, 1_000_000, 1_000_000) > 0, f"{provider}/{model} unpriced"


# --- cache keys ------------------------------------------------------------


def test_identical_requests_share_a_key():
    a = cache_key(req(), Provider.google, "gemini-2.0-flash")
    b = cache_key(req(), Provider.google, "gemini-2.0-flash")
    assert a == b


def test_temperature_is_part_of_the_key():
    """Two requests differing only in temperature are two different requests."""
    hot = cache_key(req(temperature=0.7), Provider.google, "gemini-2.0-flash")
    cold = cache_key(req(temperature=0.0), Provider.google, "gemini-2.0-flash")
    assert hot != cold


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_tokens": 512},
        {"system": "be terse"},
        {"messages": [Message(role="user", content="different")]},
    ],
)
def test_any_semantic_difference_changes_the_key(overrides):
    base = cache_key(req(), Provider.google, "x")
    assert base != cache_key(req(**overrides), Provider.google, "x")


def test_provider_and_model_are_part_of_the_key():
    """Serving Gemini's cached answer as Anthropic's would be a correctness bug."""
    assert cache_key(req(), Provider.google, "m") != cache_key(req(), Provider.anthropic, "m")
    assert cache_key(req(), Provider.google, "m1") != cache_key(req(), Provider.google, "m2")


def test_policy_is_not_part_of_the_key():
    """Two policies that land on the same provider should share cached answers."""
    a = cache_key(req(policy=RoutePolicy.cheapest), Provider.google, "m")
    b = cache_key(req(policy=RoutePolicy.fastest), Provider.google, "m")
    assert a == b
