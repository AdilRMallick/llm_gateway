"""Request orchestration: cache → route → attempt → failover → account.

Failover policy, stated once here so it can be argued with:

* We retry the *same* provider before moving on, with exponential backoff and
  jitter. A 429 or a 503 is usually a queue that clears in tens of milliseconds;
  jumping providers immediately means abandoning a warm, cheap, already-chosen
  path for a more expensive one on the strength of one bad sample. The cost of
  waiting is bounded (`backoff_max_s`); the cost of flapping is not.
* A 429 with `Retry-After` is honoured up to the backoff ceiling — the provider
  told us when to come back, and ignoring it is how you get rate-limited harder.
* A 4xx that is not a 429 is never retried on the same provider. The request is
  malformed for that provider and will stay malformed. We do move to the next
  provider, because "this model does not exist here" is a per-provider fact.
* `pinned` never falls through. See router.py.
"""

import asyncio
import random
import time
import uuid

import httpx
import structlog

from app.accounting import AccountingWriter
from app.adapters import Adapter
from app.cache import ResponseCache, cache_key
from app.config import Settings
from app.errors import ErrorKind, NoProviderAvailable, ProviderError
from app.health import ProviderHealth
from app.models import RequestRecord
from app.pricing import cost_usd, estimate_tokens
from app.router import Candidate, Router, prompt_tokens_estimate
from app.schemas import (
    Attempt,
    ChatRequest,
    ChatResponse,
    Provider,
    ProviderCompletion,
    Usage,
)

log = structlog.get_logger(__name__)


class Gateway:
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient,
        adapters: dict[Provider, Adapter],
        cache: ResponseCache,
        health: ProviderHealth,
        router: Router,
        accounting: AccountingWriter,
    ):
        self.s = settings
        self.client = client
        self.adapters = adapters
        self.cache = cache
        self.health = health
        self.router = router
        self.accounting = accounting

    async def chat(self, req: ChatRequest) -> ChatResponse:
        request_id = str(uuid.uuid4())
        t0 = time.perf_counter()
        plan = await self.router.plan(req)
        if not plan:
            raise NoProviderAvailable("no providers configured", [])

        key = cache_key(req, plan[0].provider, plan[0].model) if self.cache.cacheable(req) else None
        lock_token: str | None = None

        if key is not None:
            cached = await self.cache.get(key)
            if cached is None:
                lock_token = await self.cache.acquire_lock(key)
                if lock_token is None:
                    # Someone else is already asking the provider this exact question.
                    cached = await self.cache.wait_for(key)
            if cached is not None:
                return await self._finish_cache_hit(request_id, req, plan[0], cached, t0)

        try:
            completion, served, attempts = await self._attempt_all(req, plan)
        except NoProviderAvailable as e:
            # Drop the lock before recording: waiters should stop waiting on a
            # holder that has nothing to give them.
            if key is not None and lock_token is not None:
                await self.cache.release_lock(key, lock_token)
            await self._record_failure(request_id, req, e.attempts, t0)
            raise

        if key is not None:
            # Write the value first, then release: a waiter that wakes on the
            # released lock must find the result already there.
            await self.cache.set(key, completion)
            if lock_token is not None:
                await self.cache.release_lock(key, lock_token)

        latency_ms = _elapsed_ms(t0)
        tokens_in, tokens_out, estimated = _resolve_usage(req, completion)
        cost = cost_usd(served.provider, completion.model, tokens_in, tokens_out)

        await self._persist(
            request_id=request_id,
            req=req,
            attempts=attempts,
            served=served,
            model_served=completion.model,
            cache_hit=False,
            latency_ms=latency_ms,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            usage_estimated=estimated,
            cost=cost,
            status="ok",
        )

        return ChatResponse(
            id=request_id,
            content=completion.content,
            provider=served.provider,
            model=completion.model,
            cache_hit=False,
            latency_ms=latency_ms,
            usage=Usage(tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost),
            attempts=attempts,
            policy=req.policy,
        )

    # ---- provider attempts -------------------------------------------------

    async def _attempt_all(
        self, req: ChatRequest, plan: list[Candidate]
    ) -> tuple[ProviderCompletion, Candidate, list[Attempt]]:
        attempts: list[Attempt] = []

        for cand in plan:
            adapter = self.adapters[cand.provider]
            for tryno in range(1, self.s.max_attempts_per_provider + 1):
                t = time.perf_counter()
                try:
                    completion = await adapter.complete(self.client, req, cand.model)
                except ProviderError as e:
                    latency = _elapsed_ms(t)
                    attempts.append(
                        Attempt(
                            provider=cand.provider,
                            model=cand.model,
                            status="error",
                            latency_ms=latency,
                            error_kind=e.kind.value,
                            http_status=e.http_status,
                        )
                    )
                    await self.health.record(
                        cand.provider,
                        latency,
                        ok=False,
                        rate_limited=e.kind is ErrorKind.rate_limited,
                    )
                    log.info(
                        "provider.attempt_failed",
                        provider=cand.provider.value,
                        attempt=tryno,
                        kind=e.kind.value,
                        http_status=e.http_status,
                        error=e.message[:200],
                    )
                    if not e.retryable or tryno == self.s.max_attempts_per_provider:
                        break  # next provider in the plan
                    await asyncio.sleep(self._backoff(tryno, e.retry_after_s))
                    continue

                latency = _elapsed_ms(t)
                attempts.append(
                    Attempt(
                        provider=cand.provider,
                        model=cand.model,
                        status="ok",
                        latency_ms=latency,
                    )
                )
                await self.health.record(cand.provider, latency, ok=True)
                return completion, cand, attempts

        raise NoProviderAvailable(
            f"all {len({a.provider for a in attempts})} provider(s) failed", attempts
        )

    def _backoff(self, tryno: int, retry_after_s: float | None) -> float:
        if retry_after_s is not None:
            return min(retry_after_s, self.s.backoff_max_s)
        # Full jitter: without it, a burst of requests that all got 429 retries in
        # lockstep and 429s again.
        ceiling = min(self.s.backoff_base_s * (2 ** (tryno - 1)), self.s.backoff_max_s)
        return random.uniform(0, ceiling)

    # ---- bookkeeping -------------------------------------------------------

    async def _finish_cache_hit(
        self,
        request_id: str,
        req: ChatRequest,
        cand: Candidate,
        cached: ProviderCompletion,
        t0: float,
    ) -> ChatResponse:
        latency_ms = _elapsed_ms(t0)
        # cost_usd is 0 on a hit: no provider call was billed. The tokens are still
        # recorded so /stats can report what the hit *would* have cost.
        await self._persist(
            request_id=request_id,
            req=req,
            attempts=[],
            served=cand,
            model_served=cached.model,
            cache_hit=True,
            latency_ms=latency_ms,
            tokens_in=cached.tokens_in,
            tokens_out=cached.tokens_out,
            usage_estimated=not cached.usage_reported,
            cost=0.0,
            status="ok",
        )
        return ChatResponse(
            id=request_id,
            content=cached.content,
            provider=cand.provider,
            model=cached.model,
            cache_hit=True,
            latency_ms=latency_ms,
            usage=Usage(tokens_in=cached.tokens_in, tokens_out=cached.tokens_out, cost_usd=0.0),
            attempts=[],
            policy=req.policy,
        )

    async def _record_failure(
        self, request_id: str, req: ChatRequest, attempts: list[Attempt], t0: float
    ) -> None:
        await self._persist(
            request_id=request_id,
            req=req,
            attempts=attempts,
            served=None,
            model_served=None,
            cache_hit=False,
            latency_ms=_elapsed_ms(t0),
            tokens_in=prompt_tokens_estimate(req),
            tokens_out=0,
            usage_estimated=True,
            cost=0.0,
            status="error",
        )

    async def _persist(
        self,
        *,
        request_id: str,
        req: ChatRequest,
        attempts: list[Attempt],
        served: Candidate | None,
        model_served: str | None,
        cache_hit: bool,
        latency_ms: int,
        tokens_in: int,
        tokens_out: int,
        usage_estimated: bool,
        cost: float,
        status: str,
    ) -> None:
        record = RequestRecord(
            request_id=request_id,
            route_policy=req.policy.value,
            provider_attempted=[a.provider.value for a in attempts],
            provider_served=served.provider.value if served else None,
            model_served=model_served,
            cache_hit=cache_hit,
            latency_ms=latency_ms,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            usage_estimated=usage_estimated,
            cost_usd=cost,
            status=status,
            failover_reason=_failover_reason(attempts),
        )
        # Non-blocking by design: see app/accounting.py.
        self.accounting.submit(record)


def _failover_reason(attempts: list[Attempt]) -> str | None:
    failed = [a for a in attempts if a.status == "error"]
    if not failed:
        return None
    parts = [
        f"{a.provider.value}:{a.error_kind}" + (f"/{a.http_status}" if a.http_status else "")
        for a in failed
    ]
    return "; ".join(parts)[:255]


def _resolve_usage(req: ChatRequest, completion: ProviderCompletion) -> tuple[int, int, bool]:
    """Fall back to a local estimate when the provider reported no usage.

    Providers do not always return a usage block — notably on a partial or errored
    generation. Rather than record 0 tokens and a $0 cost that is silently wrong,
    we estimate from the text and flag the row.
    """
    if completion.usage_reported and (completion.tokens_in or completion.tokens_out):
        return completion.tokens_in, completion.tokens_out, False
    tokens_in = completion.tokens_in or prompt_tokens_estimate(req)
    tokens_out = completion.tokens_out or estimate_tokens(completion.content)
    return tokens_in, tokens_out, True


def _elapsed_ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)
