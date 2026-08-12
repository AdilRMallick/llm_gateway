"""Rolling per-provider latency and error rate, used by the `fastest` policy.

Samples live in a capped Redis list so every gateway replica routes off the same
observations. If Redis is unavailable we fall back to a per-process deque: the
window gets narrower, not wrong, and `fastest` keeps working.
"""

import time
from collections import defaultdict, deque

import redis.asyncio as aioredis
import structlog
from redis.exceptions import RedisError

from app.config import Settings
from app.schemas import Provider

log = structlog.get_logger(__name__)


class ProviderHealth:
    def __init__(self, redis: aioredis.Redis | None, settings: Settings):
        self.redis = redis
        self.s = settings
        self._local: dict[Provider, deque[tuple[float, float, bool]]] = defaultdict(
            lambda: deque(maxlen=settings.health_max_samples)
        )

    @staticmethod
    def _key(provider: Provider) -> str:
        return f"health:{provider.value}"

    async def record(
        self, provider: Provider, latency_ms: float, ok: bool, rate_limited: bool = False
    ) -> None:
        now = time.time()
        self._local[provider].append((now, latency_ms, ok))
        if self.redis is None:
            return
        try:
            pipe = self.redis.pipeline()
            pipe.lpush(self._key(provider), f"{now}:{latency_ms:.3f}:{int(ok)}:{int(rate_limited)}")
            pipe.ltrim(self._key(provider), 0, self.s.health_max_samples - 1)
            await pipe.execute()
        except RedisError as e:
            log.warning("health.record_failed", provider=provider.value, error=str(e))

    async def _samples(self, provider: Provider) -> list[tuple[float, bool, bool]]:
        """(latency_ms, ok, rate_limited) inside the window, newest first."""
        cutoff = time.time() - self.s.health_window_s
        if self.redis is not None:
            try:
                raw = await self.redis.lrange(self._key(provider), 0, self.s.health_max_samples - 1)
                out = []
                for item in raw:
                    ts, lat, ok, rl = item.split(":")
                    if float(ts) >= cutoff:
                        out.append((float(lat), ok == "1", rl == "1"))
                return out
            except (RedisError, ValueError) as e:
                log.warning("health.read_failed", provider=provider.value, error=str(e))
        return [(lat, ok, False) for ts, lat, ok in self._local[provider] if ts >= cutoff]

    async def stats(self, provider: Provider) -> dict:
        samples = await self._samples(provider)
        if not samples:
            return {
                "provider": provider.value,
                "samples": 0,
                "p50_ms": None,
                "p95_ms": None,
                "error_rate": None,
                "rate_limited_count": 0,
                "window_s": self.s.health_window_s,
            }
        latencies = sorted(lat for lat, ok, _ in samples if ok)
        errors = sum(1 for _, ok, _ in samples if not ok)
        return {
            "provider": provider.value,
            "samples": len(samples),
            "p50_ms": round(_percentile(latencies, 0.50), 2) if latencies else None,
            "p95_ms": round(_percentile(latencies, 0.95), 2) if latencies else None,
            "error_rate": round(errors / len(samples), 4),
            "rate_limited_count": sum(1 for _, _, rl in samples if rl),
            "window_s": self.s.health_window_s,
        }

    async def score(self, provider: Provider) -> float:
        """Lower is better: expected milliseconds per *successful* answer.

        `p50 / success_rate` rather than raw p50, because a provider that answers
        half your calls is worth half its apparent speed — the other half cost a
        retry and a fallthrough. That is a quantity with a meaning, not a tuning
        constant: at a 20% error rate a provider needs to be 25% faster to stay
        ahead of a reliable one.

        The success rate is floored at 0.05 so a totally dead provider sorts last
        but still sorts — being tried occasionally is how it gets to recover.

        A provider with no samples scores 0 and is tried first, which is what you
        want on a cold start: it is how the window gets filled.
        """
        st = await self.stats(provider)
        if not st["samples"]:
            return 0.0
        p50 = st["p50_ms"] if st["p50_ms"] is not None else float(self.s.request_timeout_s * 1000)
        return p50 / max(1.0 - (st["error_rate"] or 0.0), 0.05)


def _percentile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, max(0, int(round(q * (len(sorted_values) - 1)))))
    return sorted_values[idx]
