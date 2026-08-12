# LLM Gateway

A single HTTP service in front of Anthropic, OpenAI, and Google. One request shape in, one
response shape out, whichever provider ends up serving it — with policy-based routing,
automatic failover, response caching, and per-request cost accounting.

```
POST /v1/chat
```

```bash
docker compose up -d
curl -s -X POST http://localhost:8000/v1/chat -H 'content-type: application/json' \
  -d '{"messages":[{"role":"user","content":"hello"}],"policy":"cheapest"}'
```

No API keys required. The providers are a fault-injecting mock service that speaks all
three real wire formats, so every number below is reproducible on a clean clone with no
spend. On Windows PowerShell use [scripts/chat.ps1](scripts/chat.ps1) — `curl` there is an
alias for `Invoke-WebRequest` and mangles JSON bodies.

---

## Results

Measured on Docker Desktop / WSL2, Linux 6.18, Python 3.12, 12 cores. The benchmark client
runs **inside** the compose network: from a Windows host, Docker's port forward adds ~70 ms
per request, which is larger than most of the effects being measured. Each script writes its
full method — run count, warmups discarded, concurrency, and the row count the database
started at — beside its output in [bench/results/](bench/results/).

### Caching: 3.5 ms on the hit path

| pass | n | p50 | p95 | p99 | mean |
|---|---|---|---|---|---|
| cold (cache miss) | 50 | 127.64 ms | 129.61 ms | 131.01 ms | 127.79 ms |
| warm (cache hit) | 200 | **3.48 ms** | **4.92 ms** | 6.74 ms | 3.65 ms |

The cold figure is dominated by the mock's configured 120 ms, so the honest claim is not
"128 ms became 3.5 ms" — it is that a hit costs 3.5 ms instead of a provider round trip,
whatever that round trip happens to cost. Against live Gemini the same request took **829 ms**
cold and **2 ms** cached. The gateway's own instrumentation agrees with the client to within
~2.5 ms, which is the HTTP and framework overhead outside the handler.

Run conditions matter enough to state: these come from a cold stack with this benchmark run
first (`docker compose down -v && docker compose up -d`). Against a stack that has already
served a few hundred requests the hit p50 roughly doubles. Same code, different conditions,
incomparable numbers — so each result file records what it started with and the benchmark
warns when the stack is warm.

### Failover: an outage caused on purpose

| phase | injected | outcome | mean latency | attempt chain |
|---|---|---|---|---|
| 1 | — | 20/20 ok | 48.81 ms | `openai` |
| 2 | openai 100% 503 | 20/20 ok | 160.09 ms | `openai→openai→google` |
| 3 | + google 100% 429 | 20/20 ok | 295.74 ms | `openai→openai→google→google→anthropic` |
| 4 | + anthropic 100% 503 | 0/20, clean 502 | 367.98 ms | all six attempts |
| 5 | restored | 20/20 ok | 49.32 ms | `openai` |

Phase 5 recovers with no operator action. The chain is not asserted from the response — it is
read back out of Postgres, where `provider_attempted` is a real array column:

```
provider_attempted   ['openai', 'openai', 'google', 'google', 'anthropic', 'anthropic']
provider_served      None
status               error
failover_reason      openai:server_error/503; openai:server_error/503;
                     google:rate_limited/429; google:rate_limited/429;
                     anthropic:server_error/503; anthropic:server_error/503
```

That latency column is also the clearest statement of this design's main weakness, discussed
under [Limitations](#limitations).

### Cost: routing as a request parameter

The same 60-request workload under each policy:

| scenario | served by | cache hits | tokens in/out | cost | per 1k requests |
|---|---|---|---|---|---|
| pinned: anthropic | anthropic ×60 | 0 | 1478 / 3406 | $0.018508 | $0.3085 |
| pinned: google | google ×60 | 0 | 1426 / 3398 | $0.008923 | $0.1487 |
| pinned: openai | openai ×60 | 0 | 1426 / 3262 | $0.002171 | $0.0362 |
| fastest | openai ×60 | 0 | 1352 / 3158 | $0.002098 | $0.0350 |
| cheapest | openai ×60 | 0 | 1358 / 3166 | $0.002103 | $0.0351 |
| **cheapest + cache** | openai ×60 | 30 | 1462 / 3272 | **$0.001091** | **$0.0182** |

88.6% below the most expensive pin, 94.1% with caching on. The arithmetic is not the
interesting part — routing to a cheaper model obviously costs less. What matters is that the
switch is a request parameter rather than a code change, and that every request carries its own
costed record, so the saving is measured rather than projected.

The workload is 60 requests drawn from 30 distinct prompts: a 50% repeat rate, because an
all-unique workload makes the cache look worthless and a single-prompt workload makes it look
magic. Policy rows run at `temperature=0.7`, above the cache threshold, so a cheaper policy has
to win on routing rather than on getting luckier with the cache.

---

## Architecture

```
client
  │
  ▼
Envoy ──────────── TLS termination, edge rate limit (100 rps), JSON access logs
  │
  ▼
FastAPI gateway
  ├── Router          orders providers by policy + live health
  ├── Cache           Redis lookup, single-flight on miss
  ├── Adapters        anthropic / openai / google → one normalized shape
  ├── Failover        jittered backoff, retry, then the next provider
  ├── Accounting      batched background writer → Postgres
  └── Health tracker  rolling p50/p95 + error rate per provider
  │
  ▼
Provider APIs  (real) ── or ── Mock provider service (benchmarks, CI)
```

| Layer | Choice |
|---|---|
| Service | Python + FastAPI, async throughout |
| HTTP client | `httpx` — the workload is I/O bound on provider calls |
| Cache + rate-limit state | Redis |
| Persistence | PostgreSQL + SQLAlchemy 2.0 (async) |
| Migrations | Alembic |
| Edge proxy | Envoy |
| Tests | pytest + Testcontainers (real Postgres, real Redis) |
| Orchestration | Docker Compose |

### The path of one request

Worth following end to end, because most of the design decisions live on it.

1. **Plan.** The router turns the request into an *ordered list* of `(provider, model)`
   candidates. Routing is pure local computation — a rate-card lookup and a health score — so
   it is free relative to the network call it precedes.
2. **Cache lookup**, keyed on the head of that list. The key must include the provider, which
   is why the plan is computed first.
3. **On a miss, take a lock** before calling anyone.
4. **Attempt** the first candidate. On a retryable failure, back off and retry the same
   provider; on exhaustion, move to the next candidate. On a non-retryable failure, skip
   straight to the next candidate.
5. **Record** the outcome to the health window, store the response, release the lock.
6. **Account** — build the row, hand it to a background writer, and return. The client never
   waits for the database.

### Data model

```
requests
  id, request_id, ts, route_policy,
  provider_attempted[], provider_served, model_served,
  cache_hit, latency_ms,
  tokens_in, tokens_out, usage_estimated, cost_usd,
  status, failover_reason
```

`provider_attempted` is a Postgres array rather than a joined table or a JSON blob. An array
is the right shape because the attempt chain is ordered, short, always read whole, and never
queried by element. It is also what makes the failover claim checkable instead of assertable:
you can go look at what actually happened to any individual request.

`usage_estimated` exists so that a cost figure is never silently wrong. See
[Counting tokens](#counting-tokens).

---

## Routing

Three policies, each returning an ordered plan rather than a single choice.

**`cheapest`** ranks by estimated cost for *this* request: prompt tokens at the input rate,
`max_tokens` at the output rate. Assuming worst-case output keeps the ordering stable across
requests instead of making it depend on how chatty a model happened to be last time. Health is
the tiebreak, so two providers at the same price are split by which one is currently answering.

**`fastest`** ranks by `p50 / success_rate` over a rolling 60-second window — expected
milliseconds per *successful* answer, not raw latency. A provider that answers half your calls
is worth half its apparent speed, because the other half cost a retry and a fallthrough. That
formula is a quantity with a meaning rather than a tuning constant: at a 20% error rate a
provider needs to be 25% faster to stay ahead of a reliable one. The success rate is floored at
0.05, so a totally dead provider sorts last but still sorts — being tried occasionally is how
it gets to recover. A provider with no samples scores 0 and is tried first, which is correct on
a cold start: it is how the window gets filled.

**`pinned`** returns exactly one candidate and never falls through. A pin that quietly serves
someone else is not a pin. It also makes the cost comparison above meaningful — pinned-to-one
has to genuinely stay on one, or the table measures nothing.

---

## Caching

### The key

A hash of the semantic request — messages, system prompt, `max_tokens`, `temperature` — plus
the provider and model that would serve it.

Temperature is in the key because two requests differing only in temperature are two different
requests. The provider is in the key because two providers do not produce interchangeable
answers; serving Gemini's cached text as Anthropic's would be a correctness bug, not an
optimization. The routing *policy* is deliberately **not** in the key, so two policies that
land on the same provider share cached answers.

Separately, nothing above `cache_max_temperature` (default 0.0) is cached at all. The caller
asked to sample; replaying one recorded sample forever quietly converts a sampled endpoint into
a deterministic one. Better to be explicit about that than clever.

### Single-flight

The interesting case is not one request — it is a hundred identical requests arriving against a
cold cache. Naively each one misses, each one calls the provider, and the cache turns a
stampede into a hundred-fold bill.

So the first misser wins a Redis `SET NX` lock and calls the provider; the rest poll for its
result. Ten concurrent identical requests produce **one** upstream call, asserted by reading the
mock's own call counter rather than trusting the gateway's report of itself.

Failure modes were the design constraint. If the holder dies, the lock's TTL expires and the
waiters proceed on their own. If it is merely slow, they give up after `cache_lock_wait_s` and
call the provider themselves — degrading to the uncoordinated behaviour rather than deadlocking.
The value is written *before* the lock is released, so a waiter woken by the release always
finds a result waiting.

### When Redis is gone

Nothing breaks. Every cache call is best-effort; a `RedisError` degrades the service to no
caching and the health tracker falls back to a per-process window. A test points the cache at a
dead port mid-run and asserts requests still return 200. `/ready` reports Redis status without
failing readiness on it — Postgres is required, the cache is not.

The one subtlety: on a cache-lock failure the waiter path checks for a result *before* sleeping,
so an outage costs zero extra latency per request rather than one poll interval.

---

## Failover

### Backoff before fallthrough

We retry the same provider first, with full-jitter exponential backoff, and only then move on.

A 429 or a 503 is usually a queue that clears in tens of milliseconds. Jumping providers on one
bad sample abandons a warm, cheap, already-chosen path for a more expensive one — and under load
every client does it simultaneously, so the second provider inherits the stampede that broke the
first. The cost of waiting is bounded by `backoff_max_s`; the cost of flapping is not. Jitter is
not decoration: without it a burst of requests that all got a 429 retries in lockstep and gets
429'd again.

`Retry-After` is honoured when the provider sends one, capped at the backoff ceiling. The
provider told us when to come back; ignoring that is how you get rate-limited harder.

The trade is tail latency. A request that ends up failing over pays the retries first — visible
in the phase 4 row above as 368 ms for a request that was always going to fail.

### The error taxonomy is the load-bearing part

Retry behaviour is driven by *why* the call failed, not by whether it failed:

| kind | trigger | retry same provider? |
|---|---|---|
| `timeout` | client timeout | yes |
| `connection` | transport failure | yes |
| `server_error` | 5xx | yes |
| `rate_limited` | 429 | yes, honouring `Retry-After` |
| `bad_response` | 2xx that will not parse | yes |
| `client_error` | 4xx other than 429 | **no** — try the next provider |

Retrying a malformed request just burns latency: it will stay malformed. But it is still worth
trying the *next* provider, because "this model does not exist here" is a per-provider fact.

One category earns a special case. `httpx.LocalProtocolError` — raised when *we* built an
invalid request — subclasses `TransportError`, so it naturally reads as "the provider is
unreachable" and gets retried. It is mapped to `client_error` explicitly, so our own bugs cannot
masquerade as someone else's outage. That mapping is not hypothetical; see
[What the mock could not catch](#what-the-mock-could-not-catch).

---

## Accounting

### Off the request path

Writing each row inline was the first implementation, and it was wrong. A durable Postgres
commit costs ~66 ms on this hardware — more than the mocked provider call — so the gateway was
charging every client for its own bookkeeping, and a 4 ms cache hit took 70 ms to return.

Rows now go onto a bounded queue that a single background task drains, batching whatever has
accumulated into one commit. Batching is what makes it cheap: the fsync is paid once per batch
instead of once per request, so throughput *improves* under load rather than degrading.

Three consequences, each a deliberate trade:

- **A full queue drops rows; it never blocks.** Accounting is not worth failing a served request
  over. Drops are counted and surfaced in `/stats`, so the number is visibly incomplete rather
  than quietly wrong.
- **Rows are not durable at response time.** A crash loses at most one batch. If this were
  billing rather than cost attribution, that trade would go the other way.
- **`/stats` and `/requests` flush before reading**, so an inspection endpoint never shows a
  stale log. That is what keeps the benchmarks honest.

### Counting tokens

Cost is tokens × the rate card in [app/pricing.py](app/pricing.py), which `/stats` stamps with
its as-of date so a figure always travels with the prices it was computed against.

When a provider returns no usage block — which happens on partial or errored generations,
exactly when no exact count exists — tokens are estimated at ~4 characters each and the row is
flagged `usage_estimated`. Recording zero tokens and $0 would be a number that is silently
wrong, which is worse than one that is visibly approximate. `/stats` reports
`estimated_usage_rows` alongside the totals so aggregates can be read with and without them.

Reasoning models need care here. Google reports thinking tokens in `thoughtsTokenCount`,
separate from `candidatesTokenCount`, and prices output *"including thinking tokens"* — so
reading only the candidates field under-reports cost badly. The adapter sums both.

---

## The edge

Envoy terminates TLS and enforces a 100 rps token bucket before anything reaches the
application.

Rate limiting belongs here rather than in the app for two reasons. A request that is going to be
rejected should be rejected before it costs a worker, a database connection, or a Redis round
trip. And the limit has to hold across every replica — three gateways with a 100 rps in-process
limiter each is a 300 rps limit, which is not the limit anyone configured. Envoy also takes TLS
and access logging off the application entirely.

Both listeners share one filter chain via a YAML anchor, so the plaintext port cannot drift from
the TLS one.

---

## Testing

56 tests, plus 3 that need the compose stack.

**Adapter units** run against recorded provider response bodies through `httpx.MockTransport` —
no containers. A schema change fails loudly here, naming the field, instead of surfacing as a
502 in an integration test.

**Routing and cache-key units** assert properties rather than today's values. `cheapest` is
checked to be *sorted by estimated cost*, not to return a specific provider — see
[Rate cards go stale](#rate-cards-go-stale) for why that matters.

**Integration** runs against real Postgres and real Redis via Testcontainers. The behaviour that
matters here — array columns, `percentile_cont`, `SET NX` semantics, TTLs — is the behaviour of
those systems, and a fake would only test the fake. Schema comes from `alembic upgrade head`
rather than `create_all`, so every run also verifies the migration still matches the models. The
providers are the real mock service mounted in-process over an ASGI transport: same code that
serves the benchmarks, no extra container, no sleeps.

**Edge tests** cover TLS, a completion through the proxy, and rate-limit shedding. The
rate-limit test measures the request rate it actually achieved and skips rather than fails when
the client could not outpace the limit — on Docker Desktop for Windows the host port forward
tops out around 35 rps, which says nothing about the limiter.

CI runs lint, the suite, and a compose job that brings up the full stack, smokes a completion,
exercises Envoy, and runs all three benchmarks. Each benchmark asserts its own expectations, so
a broken failover chain fails the build rather than quietly producing a different number.

### Why a mock provider at all

[mock_provider/main.py](mock_provider/main.py) speaks all three real schemas with per-provider
injectable latency, error rate, 429 rate, 400 rate, and usage suppression.

```bash
curl -X POST localhost:9000/_control/openai -d '{"error_rate": 1.0}'   # take openai down
curl -X POST localhost:9000/_control/reset                             # bring it back
curl localhost:9000/_control                                           # knobs + call counts
```

It makes benchmarks deterministic for anyone who clones the repo, lets CI run without keys or
spend, and turns "failover works" into something demonstrable on demand rather than something
you wait for an outage to prove.

### What the mock could not catch

It also has a specific, structural blind spot, and pointing the gateway at a live API exposed it:
**the mock faithfully reproduces the schema you believed in.**

Three real bugs it could never have found:

- **Output tokens under-counted ~12×.** `thoughtsTokenCount` is reported separately from
  `candidatesTokenCount` and is billable. An 8-token prompt to `gemini-3.6-flash` returned
  `candidatesTokenCount: 2` and `thoughtsTokenCount: 24`.
- **Every Google model in the rate card was retired.** `gemini-2.0-flash` 404s with "no longer
  available"; so does the entire 2.5 series on a new key.
- **The OpenAI adapter never worked.** With no key configured it sent `Bearer ` — trailing space
  — which httpx rejects as an illegal header value. Because `LocalProtocolError` subclasses
  `TransportError`, this surfaced as a transport failure and got *retried twice* before falling
  through, looking exactly like an outage. The fix was two parts: omit the header when there is
  no key, and stop letting local protocol errors impersonate remote ones.

---

## Rate cards go stale

[app/pricing.py](app/pricing.py) is data, not logic, and it decays. Two things follow.

**Models retire.** The Google entries were read off the live pricing page on the `PRICING_AS_OF`
date. **The OpenAI and Anthropic entries have not been verified against a live account** — and
given that every Google entry turned out to be stale, treat `gpt-4o-mini`, `claude-haiku-4-5`,
and their rates as needing a check before quoting any cost figure that involves them. An
unpriced model costs $0.00 by design: visibly wrong beats silently wrong.

**A price change reorders routing.** When Google's replacement model came in above
`gpt-4o-mini`, cheapest-first flipped from google to openai and fourteen tests failed for a
reason that had nothing to do with the code they covered. Tests and the failover demo now derive
the expected order *from the rate card*, so a price change updates them rather than breaking
them. The same reasoning applies to the linter: it is pinned in exactly one place, because a
lint job that disagrees with local lint is worse than no lint job.

---

## Limitations

Worth being direct about, since the failover table above shows the first one plainly.

**There is no circuit breaker.** In phase 2, openai is known dead — the health tracker recorded
every failure — and all 20 requests still try it twice before falling through. Latency stays at
160 ms for the entire outage instead of returning to baseline after the first few requests.
Health feeds `fastest` only; under `cheapest` it is a tiebreak, so a dead-but-cheapest provider
is re-probed on every request indefinitely. Health was scoped as ordering input, not admission
control. Trip after N consecutive failures with a half-open probe after a cooldown and phase 2
would drop back toward 49 ms — the most valuable next change here.

**The health window is per-replica in practice.** Samples are shared through Redis, but a
Redis outage silently narrows each replica to its own view.

**No streaming.** A gateway that buffers whole responses is the wrong shape for chat UIs; the
cache and the accounting model both assume a complete response.

**Cost is attribution, not billing.** Dropped rows and estimated usage are acceptable here and
would not be if anyone were invoiced from this data.

---

## API

| endpoint | purpose |
|---|---|
| `POST /v1/chat` | completions; provider-agnostic in and out |
| `GET /health` | liveness — dependency-free, the process is up |
| `GET /ready` | readiness — Postgres required, Redis is not |
| `GET /stats?window_minutes=60` | totals, per-provider, per-policy, latency by cache hit, live health |
| `GET /requests?limit=20&failed_only=` | the request log, including the attempt chain |

```jsonc
// request
{
  "messages": [{"role": "user", "content": "..."}],
  "system": "optional",
  "max_tokens": 512,
  "temperature": 0.0,
  "policy": "cheapest",            // cheapest | fastest | pinned
  "provider": null,                // required when policy = pinned
  "models": {"openai": "gpt-4o"}   // optional per-provider override
}

// response
{
  "id": "...", "content": "...",
  "provider": "openai", "model": "gpt-4o-mini",
  "cache_hit": false, "latency_ms": 48,
  "usage": {"tokens_in": 11, "tokens_out": 35, "cost_usd": 0.0000225},
  "attempts": [{"provider": "openai", "status": "ok", "latency_ms": 42}],
  "policy": "cheapest"
}
```

---

## Development

```bash
pip install -e ".[dev]"

pytest                      # Testcontainers brings up Postgres + Redis; no keys needed
ruff check . && ruff format --check .

docker compose up -d
docker compose run --build --rm bench python -m bench.failover_demo
docker compose run --build --rm bench python -m bench.cache_bench
docker compose run --build --rm bench python -m bench.cost_compare

EDGE_URL=http://localhost:8080 EDGE_TLS_URL=https://localhost:8443 pytest tests/test_edge.py
```

Pass `--build` to `compose run`: the bench service sits behind a profile, so a plain
`up --build` skips it and `run` will happily reuse a stale image.

### Against a real provider

```bash
echo 'GATEWAY_GOOGLE_API_KEY=...' >> .env
docker compose -f docker-compose.yml -f docker-compose.live.yml up -d gateway
```

`.env` is gitignored and never copied into the image; Compose loads it only for `${VAR}`
substitution, and [docker-compose.live.yml](docker-compose.live.yml) passes the value through
while clearing the base URL so the adapter falls back to the real endpoint. The other two
providers stay mocked, which is the most useful shape for testing — a live provider with a
mocked fallback chain behind it.

### Layout

```
app/
  main.py          app assembly and lifespan
  gateway.py       orchestration: cache -> route -> attempt -> failover -> account
  router.py        policy engine
  adapters/        one file per provider wire format
  cache.py         Redis response cache + single-flight
  accounting.py    batched background writer
  health.py        rolling latency/error window
  pricing.py       rate card + token estimation
  routes/          /v1/chat, /stats, /requests, /health, /ready
mock_provider/     fault-injecting stand-in for all three providers
bench/             the three results above, each with its method
tests/             adapter units, routing units, integration, edge
envoy/             TLS + edge rate limit config
alembic/           migrations
```
