"""Redis response cache with single-flight.

Two design decisions worth defending:

*Key.* The key is a hash of the semantic request — messages, system prompt,
max_tokens, temperature — plus the provider and model that would serve it. The
generation params are in the key because a request differing only in temperature
is a different request; the provider is in the key because two providers do not
produce interchangeable answers, so serving Gemini's cached text as Anthropic's
would be a correctness bug, not an optimization.

*Temperature.* Above `cache_max_temperature` (default 0.0) we do not cache at all.
The caller asked to sample; replaying one recorded sample forever is the wrong
semantics, and it is worth being explicit about that rather than quietly turning a
sampled endpoint into a deterministic one.

*Redis down.* Every call here is best-effort. A Redis outage degrades the gateway
to no caching and nothing else — no request fails because the cache is unavailable.
"""

import asyncio
import hashlib
import json
import time
import uuid

import redis.asyncio as aioredis
import structlog
from redis.exceptions import RedisError

from app.config import Settings
from app.schemas import ChatRequest, Provider, ProviderCompletion

log = structlog.get_logger(__name__)

_RELEASE_IF_MINE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
else
  return 0
end
"""


def cache_key(req: ChatRequest, provider: Provider, model: str) -> str:
    payload = {
        "v": 1,
        "provider": provider.value,
        "model": model,
        "system": req.system,
        "messages": [[m.role, m.content] for m in req.messages],
        "max_tokens": req.max_tokens,
        "temperature": req.temperature,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "resp:" + hashlib.sha256(blob.encode()).hexdigest()


class ResponseCache:
    def __init__(self, redis: aioredis.Redis | None, settings: Settings):
        self.redis = redis
        self.s = settings

    @property
    def enabled(self) -> bool:
        return self.s.cache_enabled and self.redis is not None

    def cacheable(self, req: ChatRequest) -> bool:
        return self.enabled and req.temperature <= self.s.cache_max_temperature

    async def get(self, key: str) -> ProviderCompletion | None:
        if not self.enabled:
            return None
        try:
            raw = await self.redis.get(key)
        except RedisError as e:
            log.warning("cache.get_failed", error=str(e))
            return None
        if raw is None:
            return None
        try:
            return ProviderCompletion.model_validate_json(raw)
        except ValueError:
            return None

    async def set(self, key: str, completion: ProviderCompletion) -> None:
        if not self.enabled:
            return
        try:
            await self.redis.set(key, completion.model_dump_json(), ex=self.s.cache_ttl_s)
        except RedisError as e:
            log.warning("cache.set_failed", error=str(e))

    async def acquire_lock(self, key: str) -> str | None:
        """Single-flight: the first misser holds the lock and calls the provider.

        Concurrent missers for the same prompt wait on the holder rather than each
        firing their own provider call — otherwise a cold cache under load costs N
        identical requests instead of one.
        """
        if not self.enabled:
            return None
        token = uuid.uuid4().hex
        try:
            won = await self.redis.set(f"lock:{key}", token, nx=True, ex=self.s.cache_lock_ttl_s)
        except RedisError as e:
            log.warning("cache.lock_failed", error=str(e))
            return None
        return token if won else None

    async def release_lock(self, key: str, token: str) -> None:
        if not self.enabled:
            return
        try:
            await self.redis.eval(_RELEASE_IF_MINE, 1, f"lock:{key}", token)
        except RedisError as e:
            log.warning("cache.unlock_failed", error=str(e))

    async def wait_for(self, key: str) -> ProviderCompletion | None:
        """Poll for the lock holder's result. Returns None on timeout, and the
        caller then just makes its own provider call — a slow holder degrades this
        to the uncoordinated behaviour, it never deadlocks a request."""
        deadline = time.monotonic() + self.s.cache_lock_wait_s
        while time.monotonic() < deadline:
            # Check before sleeping: the holder may already have finished, and if
            # Redis is down we want to fall through to the provider immediately
            # rather than pay a poll interval per request for the whole outage.
            hit = await self.get(key)
            if hit is not None:
                return hit
            try:
                if not await self.redis.exists(f"lock:{key}"):
                    return None  # holder died or failed; go call the provider
            except RedisError:
                return None
            await asyncio.sleep(self.s.cache_lock_poll_s)
        return None


async def make_redis(settings: Settings) -> aioredis.Redis | None:
    try:
        client = aioredis.from_url(settings.redis_url, decode_responses=True)
        await client.ping()
        return client
    except Exception as e:  # noqa: BLE001 - a dead cache must not stop startup
        log.warning("redis.unavailable", error=str(e), detail="running without cache")
        return None
