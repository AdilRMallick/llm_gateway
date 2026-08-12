# LLM Gateway

One HTTP interface across Anthropic, OpenAI, and Google, with policy-based routing,
automatic failover, response caching, and per-request cost accounting.

```
POST /v1/chat  ->  same request body, same response shape, whichever provider served it
```

Everything below is reproducible on a clean clone with no API keys and no spend: the
providers are a fault-injecting mock service that speaks all three real schemas, so the
benchmarks are deterministic and the outages are caused on purpose.

```bash
git clone <this repo> && cd llm_gateway
docker compose up -d
curl -s -X POST http://localhost:8000/v1/chat \
  -H 'content-type: application/json' \
  -d '{"messages":[{"role":"user","content":"hello"}],"policy":"cheapest"}'
```

On Windows PowerShell, `curl` is an alias for `Invoke-WebRequest` and will mangle the
JSON body. Use [scripts/chat.ps1](scripts/chat.ps1) instead:

```powershell
.\scripts\chat.ps1 "hello"
.\scripts\chat.ps1 "hello" -Policy pinned -Provider google -Raw
```

---

## The three numbers

All measured on Docker Desktop / WSL2 (Linux 6.18, Python 3.12, 12 cores). The benchmark
client runs **inside** the compose network — from a Windows host, Docker's port forward
adds ~70 ms per request, which is larger than most of the effects being measured. Every
script writes its full method, including the host it ran on, next to its results in
[bench/results/](bench/results/).

**Run conditions matter, so state them.** These come from a cold stack with the cache
benchmark run first:

```bash
docker compose down -v && docker compose up -d
docker compose run --build --rm bench python -m bench.cache_bench
```

Run the same benchmark against a stack that has already served a few hundred requests
and the cache-hit p50 roughly doubles (3.5 ms → 7.9 ms, measured), which halves the
headline multiple. Nothing is wrong in either case — it is the same code — but the two
numbers are not comparable, so each result file records the row count it started with
and the benchmark warns when the stack is warm. The cost and failover artifacts are
deterministic and reproduce identically either way.

### 1. Response caching — 128 ms → 3.5 ms p50

| pass | n | p50 | p95 | p99 | mean |
|---|---|---|---|---|---|
| cold (cache miss) | 50 | 127.64 ms | 129.61 ms | 131.01 ms | 127.79 ms |
| warm (cache hit) | 200 | **3.48 ms** | **4.92 ms** | 6.74 ms | 3.65 ms |

**37× faster at p50, 26× at p95.** The gateway's own view agrees: `/stats` reports
1 ms p50 for hits against 125 ms for misses, and the ~2.5 ms gap is HTTP and framework
overhead outside the handler.

*Method:* 50 distinct prompts, each tagged with a per-run nonce so every cold call is a
genuine miss without flushing Redis. 10 warmup requests discarded — they pay for
connection setup and the asyncpg pool, which is not what is being measured. Cold pass:
each prompt once. Warm pass: the same 50 prompts replayed 4×. Mock provider pinned at
120 ms with zero jitter, so the cold number is dominated by a known, reproducible cost.
Sequential client. Reproduce: `docker compose run --build --rm bench python -m bench.cache_bench`

### 2. Failover — an outage, caused on purpose

```
phase 1  all healthy              20/20 ok   served by openai        chain: openai
phase 2  openai 100% 503          20/20 ok   served by google        chain: openai->openai->google
phase 3  + google 100% 429        20/20 ok   served by anthropic     chain: openai->openai->google->google->anthropic
phase 4  + anthropic 100% 503      0/20 ok   502 to the client       chain: all six attempts
phase 5  restored                 20/20 ok   served by openai        chain: openai
```

Recovery in phase 5 needs no operator action. The chain is not asserted, it is read back
out of Postgres — here is a real row from phase 4:

```
provider_attempted   ['openai', 'openai', 'google', 'google', 'anthropic', 'anthropic']
provider_served      None
status               error
latency_ms           329
failover_reason      openai:server_error/503; openai:server_error/503;
                     google:rate_limited/429; google:rate_limited/429;
                     anthropic:server_error/503; anthropic:server_error/503
```

*Method:* 20 requests per phase at `temperature=0.7`, which is above
`cache_max_temperature` and so guarantees every request actually reaches a provider —
a cache hit would hide the behaviour being demonstrated. Faults injected through the
mock's control plane. Which provider gets taken down first is read out of the rate card
rather than hardcoded, so the demo keeps demonstrating the same thing when prices move.
Reproduce:
`docker compose run --build --rm bench python -m bench.failover_demo`

### 3. Routing cost — 89% cheaper than pinning the expensive provider

The same 60-request workload under each policy:

| scenario | served by | cache hits | tokens in/out | cost | per 1k requests | vs baseline |
|---|---|---|---|---|---|---|
| pinned: anthropic | anthropic ×60 | 0 | 1478 / 3406 | $0.018508 | $0.3085 | baseline |
| pinned: google | google ×60 | 0 | 1426 / 3398 | $0.008923 | $0.1487 | −51.8% |
| pinned: openai | openai ×60 | 0 | 1426 / 3262 | $0.002171 | $0.0362 | −88.3% |
| fastest | google ×60 | 0 | 1352 / 3322 | $0.008711 | $0.1452 | −52.9% |
| cheapest | openai ×60 | 0 | 1358 / 3166 | $0.002103 | $0.0351 | −88.6% |
| **cheapest + cache** | openai ×60 | 30 | 1462 / 3272 | **$0.001091** | **$0.0182** | **−94.1%** |

*Method:* one seeded workload (seed 42) of 60 requests drawn from 30 distinct prompts —
a 50% repeat rate, because a workload of all-unique prompts makes the cache look
worthless and a workload of one prompt makes it look magic. The same sequence is
replayed for every scenario, so the only variable is the routing decision. The policy
rows run at `temperature=0.7` so nothing is cached and a cheaper policy has to win on
routing rather than on getting luckier with the cache; the last row runs the same
workload at `temperature=0.0` with the cache on. Costs are summed from the per-request
accounting rows, priced from the rate card in [app/pricing.py](app/pricing.py), which
`/stats` stamps with its as-of date. Reproduce:
`docker compose run --build --rm bench python -m bench.cost_compare`

Three honest caveats. Token counts differ by a percent or two between providers because
the mock echoes the provider name into its response, so the strings are not byte-identical.
`fastest` picked google here rather than openai — it routes on the observed latency
window, so which provider it picks depends on what the window contains at that moment.
And the Google rates below are verified against the live pricing page; the OpenAI and
Anthropic rates are not, because I have no keys for those providers — see
[Rate card accuracy](#rate-card-accuracy).

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
  ├── Adapters        anthropic / openai / google -> one normalized shape
  ├── Failover        backoff + retry, then the next provider
  ├── Accounting      batched background writer -> Postgres
  └── Health tracker  rolling p50/p95 + error rate per provider
  │
  ▼
Provider APIs  (real) ── or ── Mock provider service (benchmarks, CI)
```

| Layer | Choice |
|---|---|
| Service | Python + FastAPI, async throughout |
| HTTP client | `httpx` — the workload is I/O bound on provider calls |
| Cache + rate limit state | Redis |
| Persistence | PostgreSQL + SQLAlchemy 2.0 (async) |
| Migrations | Alembic |
| Edge proxy | Envoy |
| Tests | pytest + Testcontainers (real Postgres, real Redis) |
| Local orchestration | Docker Compose |

### The mock provider is the load-bearing piece

[mock_provider/main.py](mock_provider/main.py) is a FastAPI app that speaks all three
providers' real request and response schemas, with per-provider injectable latency,
error rate, 429 rate, 400 rate, and usage-block suppression. It exists so that
benchmarks are deterministic for anyone who clones the repo, CI runs without API keys or
spend, and failover can be demonstrated on demand instead of waiting for a real outage.

```bash
curl -X POST localhost:9000/_control/google -d '{"error_rate": 1.0}'   # take google down
curl -X POST localhost:9000/_control/reset                             # bring it back
curl localhost:9000/_control                                           # knobs + call counts
```

---

## API

| endpoint | what it does |
|---|---|
| `POST /v1/chat` | the completion endpoint; provider-agnostic in and out |
| `GET /health` | liveness — dependency-free, the process is up |
| `GET /ready` | readiness — Postgres required, Redis is not |
| `GET /stats?window_minutes=60` | totals, per-provider, per-policy, latency by cache hit, live provider health |
| `GET /requests?limit=20&failed_only=` | the request log, including the `provider_attempted` chain |

Request:

```jsonc
{
  "messages": [{"role": "user", "content": "..."}],
  "system": "optional",
  "max_tokens": 512,
  "temperature": 0.0,
  "policy": "cheapest",            // cheapest | fastest | pinned
  "provider": null,                // required when policy = pinned
  "models": {"openai": "gpt-4o"}   // optional per-provider model override
}
```

Response:

```jsonc
{
  "id": "…", "content": "…",
  "provider": "google", "model": "gemini-2.0-flash",
  "cache_hit": false, "latency_ms": 128,
  "usage": {"tokens_in": 11, "tokens_out": 35, "cost_usd": 0.0000151},
  "attempts": [{"provider": "google", "status": "ok", "latency_ms": 126, "…": null}],
  "policy": "cheapest"
}
```

### Routing policies

- **`cheapest`** — orders providers by estimated cost for this request (prompt tokens at
  the input rate, `max_tokens` at the output rate), health as the tiebreak. Worst-case on
  output, so the ordering is stable rather than depending on how chatty a model was last time.
- **`fastest`** — orders by expected milliseconds per *successful* answer: `p50 / success_rate`
  over a rolling 60-second window. A provider that answers half your calls is worth half
  its apparent speed. A provider with no samples sorts first — that is how the window fills.
- **`pinned`** — exactly one provider, no fallthrough. A pin that quietly serves someone
  else is not a pin, and the cost comparison above only means something if pinned-to-one
  really stayed on one.

---

## Questions this has to survive

**What happens when two requests miss the cache for the same prompt simultaneously?**
One of them wins a Redis `SET NX` lock and calls the provider; the others poll for its
result. Ten concurrent identical requests produce one upstream call — asserted in
`test_concurrent_misses_for_the_same_prompt_make_one_provider_call`, which reads the
mock's call counter rather than trusting the gateway's own report. If the holder dies,
the lock TTL expires and the waiters proceed on their own; if it is merely slow, they
give up after `cache_lock_wait_s` and call the provider themselves. A slow holder
degrades this to the uncoordinated behaviour, it never deadlocks a request.

**How do you cache-key requests that differ only in `temperature`?**
As different requests — temperature is in the key, along with the messages, system
prompt, `max_tokens`, and the provider and model that would serve it. The provider is in
the key because two providers do not produce interchangeable answers, so serving Gemini's
cached text as Anthropic's would be a correctness bug rather than an optimization.
Separately, anything above `cache_max_temperature` (default 0.0) is not cached at all:
the caller asked to sample, and replaying one recorded sample forever is the wrong
semantics. Better to be explicit about that than to quietly turn a sampled endpoint into
a deterministic one.

**Why backoff and not immediate failover?**
A 429 or a 503 is usually a queue that clears in tens of milliseconds. Jumping providers
on one bad sample abandons a warm, cheap, already-chosen path for a more expensive one,
and under load every client does it at once — so the second provider gets the stampede
that broke the first. The cost of waiting is bounded by `backoff_max_s`; the cost of
flapping is not. So: retry the same provider with full-jitter exponential backoff, honour
`Retry-After` when the provider sends one, then fall through. The trade is tail latency —
a request that ends up failing over pays the retries first, visible in phase 4 above as
371 ms for a request that was always going to fail.

**How do you count tokens for a provider that doesn't return usage on error?**
Estimate at ~4 characters per token and mark the row `usage_estimated = true`. The
alternative — recording zero tokens and a $0 cost — is a number that is silently wrong,
which is worse than one that is visibly approximate. `/stats` reports
`estimated_usage_rows` alongside the totals so the cost figure can be read with and
without them.

**Are you sure you're counting all the billable tokens?**
Not until it was checked against a live account, no. Google reports reasoning tokens in
`thoughtsTokenCount`, separate from `candidatesTokenCount`, and prices output
"including thinking tokens" — so reading only `candidatesTokenCount` under-reports cost.
On a real call to `gemini-3.6-flash`, an 8-token prompt returned
`candidatesTokenCount: 2` and `thoughtsTokenCount: 24`: a 12× under-count, silently, on
exactly the models people reach for. [google.py](app/adapters/google.py) now adds both.
This is the argument for having built the real-provider path at all — the mock
faithfully reproduces the schema I *believed* in, which means it could never have caught
this.

**What breaks if Redis goes down?**
Nothing. Every cache call is best-effort and a `RedisError` degrades the gateway to
no caching, with the health tracker falling back to a per-process window. Asserted in
`test_redis_outage_degrades_to_no_caching_and_nothing_else`, which points the cache at a
dead port mid-test and checks that requests still return 200. `/ready` reflects Redis
status without failing readiness on it.

**Why Envoy instead of handling rate limiting in the app?**
A request that is going to be rejected should be rejected before it costs a worker, a
database connection, or a Redis round trip. And the limit has to hold across every
gateway replica, which an in-process limiter cannot do — three replicas with a 100 rps
limiter each is a 300 rps limit. Envoy also takes TLS termination and access logging off
the application entirely.

**Why is the accounting write not on the request path?**
It was, at first. A durable Postgres commit costs ~66 ms on this machine — more than the
mocked provider call — so the gateway was charging every client for its own bookkeeping,
and a cache hit that took 4 ms of real work took 70 ms to return. Rows now go onto a
bounded queue and a background task batches them into one commit, so the fsync is paid
once per batch instead of once per request and throughput improves with load rather than
degrading. The trade is durability: a crash loses at most one batch, and a full queue
drops rows rather than blocking a served request. Drops are counted and surfaced in
`/stats`, so the number is visibly incomplete rather than quietly wrong. If this were
billing rather than cost attribution, that trade would go the other way.

---

## Development

```bash
pip install -e ".[dev]"

pytest                      # Testcontainers brings up Postgres + Redis; no API keys needed
ruff check . && ruff format --check .

docker compose up -d
docker compose run --build --rm bench python -m bench.cache_bench
docker compose run --build --rm bench python -m bench.failover_demo
docker compose run --build --rm bench python -m bench.cost_compare

EDGE_URL=http://localhost:8080 EDGE_TLS_URL=https://localhost:8443 pytest tests/test_edge.py
```

CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs lint, the full pytest
suite, and a `stack` job that brings up Compose, smokes a completion, exercises Envoy's
TLS and rate limiting, and runs all three benchmark scripts — every one of which asserts
its own expectations, so a broken failover chain fails the build rather than producing a
quieter number.

### Talking to a real provider

Put your key in `.env` (gitignored), then bring the gateway up with the live overlay:

```bash
echo 'GATEWAY_GOOGLE_API_KEY=...' >> .env
docker compose -f docker-compose.yml -f docker-compose.live.yml up -d gateway

curl -s -X POST http://localhost:8000/v1/chat -H 'content-type: application/json' \
  -d '{"messages":[{"role":"user","content":"hello"}],"policy":"pinned","provider":"google"}'
```

The app never reads `.env` itself — `.env` is deliberately not copied into the image.
Compose auto-loads it for `${VAR}` substitution, and
[docker-compose.live.yml](docker-compose.live.yml) is what passes the value through and
clears `GATEWAY_GOOGLE_BASE_URL` so the adapter falls back to the real endpoint. The
other two providers stay mocked, which is the most useful shape for testing: a real
Google with a mocked fallback chain behind it.

A real call, through the gateway:

```jsonc
{"provider": "google", "model": "gemini-3.5-flash-lite",
 "cache_hit": false, "latency_ms": 829,
 "usage": {"tokens_in": 12, "tokens_out": 94, "cost_usd": 0.0002386}}
```

Repeat it and you get `cache_hit: true`, 2 ms, `cost_usd: 0.0`.

### Rate card accuracy

[app/pricing.py](app/pricing.py) is data, not logic, and it goes stale. Two things it
has already taught:

- **Models retire.** `gemini-2.0-flash` returns 404 — "no longer available" — and so did
  every 2.5-series model on a new key. The current Google entries were read off
  ai.google.dev/gemini-api/docs/pricing on the `PRICING_AS_OF` date. **The OpenAI and
  Anthropic entries have not been verified against a live account**, so treat
  `gpt-4o-mini` and `claude-haiku-4-5` and their rates as needing a check before you
  quote a cost number that involves them.
- **A price change reorders routing.** When Google's replacement model came in above
  `gpt-4o-mini`, cheapest-first flipped from google to openai and eleven tests failed for
  a reason unrelated to the code they covered. Tests and the failover demo now derive the
  expected order from the rate card, so a price change updates them rather than breaking
  them. An unpriced model costs $0.00 by design — visibly wrong beats silently wrong.

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
bench/             the three artifacts above, with their methods
tests/             adapter units, routing/cache-key units, Testcontainers integration, Envoy edge
envoy/             TLS + edge rate limit config
alembic/           migrations
```

### Not in scope

Streaming responses, a UI, tool calling, RAG, multi-tenancy, and Kubernetes. Each was
left out deliberately; `/stats` returning JSON is enough.
