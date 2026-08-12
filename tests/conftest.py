"""Integration fixtures.

Real Postgres and real Redis via Testcontainers — the interesting behaviour here
(array columns, percentile queries, SETNX single-flight, TTLs) is behaviour of
those systems, and a fake would test the fake.

The providers are the actual `mock_provider` FastAPI app, mounted in-process over
an ASGI transport rather than run as a fourth container. Same code that serves the
benchmarks, no extra ports, no sleeps.

Schema comes from `alembic upgrade head`, not `create_all`, so every run also
checks that the migration still matches the models.
"""

import asyncio
import os
from collections.abc import AsyncIterator

import httpx
import pytest
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer

pytest_plugins = ()


@pytest.fixture(scope="session")
def event_loop_policy():
    return asyncio.get_event_loop_policy()


@pytest.fixture(scope="session")
def postgres() -> AsyncIterator[str]:
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="session")
def redis_url() -> AsyncIterator[str]:
    with RedisContainer("redis:7-alpine") as rc:
        yield f"redis://{rc.get_container_host_ip()}:{rc.get_exposed_port(6379)}/0"


@pytest.fixture(scope="session", autouse=True)
def _settings_env(postgres: str, redis_url: str):
    """Point the app at the containers before anything imports settings."""
    os.environ["GATEWAY_DATABASE_URL"] = postgres
    os.environ["GATEWAY_REDIS_URL"] = redis_url
    os.environ["GATEWAY_ANTHROPIC_BASE_URL"] = "http://mock/anthropic"
    os.environ["GATEWAY_OPENAI_BASE_URL"] = "http://mock/openai"
    os.environ["GATEWAY_GOOGLE_BASE_URL"] = "http://mock/google"
    # Short waits keep the failover tests fast without changing what they prove.
    os.environ["GATEWAY_BACKOFF_BASE_S"] = "0.01"
    os.environ["GATEWAY_BACKOFF_MAX_S"] = "0.05"
    os.environ["GATEWAY_CACHE_LOCK_WAIT_S"] = "5.0"

    from app.config import get_settings

    get_settings.cache_clear()

    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")
    yield


@pytest.fixture
async def mock_client() -> AsyncIterator[httpx.AsyncClient]:
    """An httpx client whose transport IS the mock provider app."""
    from mock_provider.main import app as mock_app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_app), base_url="http://mock", timeout=30.0
    ) as client:
        await client.post("/_control/reset")
        # Latency off by default: these tests assert on behaviour, not timing.
        for p in ("anthropic", "openai", "google"):
            await client.post(f"/_control/{p}", json={"latency_ms": 0.0})
        yield client
        await client.post("/_control/reset")


@pytest.fixture
async def app_client(mock_client: httpx.AsyncClient) -> AsyncIterator[httpx.AsyncClient]:
    """The real FastAPI app, with its provider client swapped for the mock."""
    from app.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        app.state.gateway.client = mock_client
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gw", timeout=30.0
        ) as client:
            client.app = app  # tests reach through for accounting/cache internals
            yield client


@pytest.fixture
async def clean_db(app_client: httpx.AsyncClient) -> AsyncIterator[None]:
    """Truncate between tests that assert on aggregates."""
    from sqlalchemy import text

    sm = app_client.app.state.sessionmaker
    async with sm() as session:
        await session.execute(text("TRUNCATE requests"))
        await session.commit()
    yield


@pytest.fixture
async def clean_cache(app_client: httpx.AsyncClient) -> AsyncIterator[None]:
    redis = app_client.app.state.redis
    if redis is not None:
        await redis.flushdb()
    yield


def cheapest_order(tokens_in: int = 16, max_tokens: int = 128) -> list[str]:
    """The provider order `cheapest` will actually try, derived from the rate card.

    Tests used to hardcode "google is cheapest". Then Google retired
    gemini-2.0-flash, its replacement cost more than gpt-4o-mini, and eleven tests
    failed for a reason that had nothing to do with the code under test. A price
    change should update these tests, not break them.
    """
    from app.pricing import DEFAULT_MODEL, estimate_cost_usd
    from app.schemas import Provider

    ranked = sorted(
        Provider, key=lambda p: estimate_cost_usd(p, DEFAULT_MODEL[p], tokens_in, max_tokens)
    )
    return [p.value for p in ranked]


def chat_body(prompt: str, **overrides) -> dict:
    body = {
        "messages": [{"role": "user", "content": prompt}],
        "policy": "cheapest",
        "temperature": 0.0,
        "max_tokens": 128,
    }
    body.update(overrides)
    return body
