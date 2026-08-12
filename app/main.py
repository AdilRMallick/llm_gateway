from contextlib import asynccontextmanager

import httpx
import structlog
from fastapi import FastAPI

from app.accounting import AccountingWriter
from app.adapters import build_adapters
from app.cache import ResponseCache, make_redis
from app.config import get_settings
from app.db import dispose_engine, get_sessionmaker
from app.gateway import Gateway
from app.health import ProviderHealth
from app.logging_config import configure_logging
from app.router import Router
from app.routes import chat, health, requests_log, stats

log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)

    timeout = httpx.Timeout(settings.request_timeout_s, connect=settings.connect_timeout_s)
    client = httpx.AsyncClient(
        timeout=timeout,
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
    )
    redis = await make_redis(settings)
    provider_health = ProviderHealth(redis, settings)

    app.state.settings = settings
    app.state.redis = redis
    app.state.client = client
    app.state.cache = ResponseCache(redis, settings)
    app.state.health = provider_health
    app.state.sessionmaker = get_sessionmaker()
    app.state.accounting = AccountingWriter(app.state.sessionmaker)
    await app.state.accounting.start()
    app.state.gateway = Gateway(
        settings=settings,
        client=client,
        adapters=build_adapters(settings),
        cache=app.state.cache,
        health=provider_health,
        router=Router(provider_health),
        accounting=app.state.accounting,
    )
    log.info("gateway.started", cache_enabled=app.state.cache.enabled)

    try:
        yield
    finally:
        # Drain accounting before tearing down the engine, so a clean shutdown
        # does not throw away rows that were already served.
        await app.state.accounting.stop()
        await client.aclose()
        if redis is not None:
            await redis.aclose()
        await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(
        title="LLM Gateway",
        version="0.1.0",
        description="One interface across Anthropic, OpenAI, and Google, with "
        "policy routing, failover, caching, and per-request cost accounting.",
        lifespan=lifespan,
    )
    app.include_router(health.router)
    app.include_router(chat.router)
    app.include_router(stats.router)
    app.include_router(requests_log.router)
    return app


app = create_app()
