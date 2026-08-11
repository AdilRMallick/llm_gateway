# LLM Gateway — Architecture Spec

A single HTTP service that sits in front of multiple LLM providers (Anthropic, OpenAI, Google) and presents one interface. Handles provider adapters, failover, caching, rate limiting, and per-request cost accounting.

**Why this one:** it is a serving and routing problem, which is what you already do at HERE, rather than a modeling problem, which you don't. Every claim it produces is reproducible on your own machine, so nothing on your resume depends on a maintainer merging anything or on a number you can't re-derive.

---

## Scope

**In scope**
- One `POST /v1/chat` endpoint, provider-agnostic request and response shape
- Adapters for 3 providers with genuinely different request/response schemas
- Routing policy: explicit provider pin, cheapest-first, or lowest-observed-latency
- Automatic failover on 5xx, timeout, and rate-limit responses
- Response caching on semantic-identical requests
- Per-request token and cost accounting, persisted and queryable
- Reverse proxy in front for TLS termination and edge rate limiting
- Benchmark suite that produces the numbers you will put on the resume

**Explicitly out of scope** — write these down and hold the line, because each one is a week you don't have:
- Streaming responses (nice, not needed for the story)
- A UI (`/stats` returning JSON is enough)
- Agent orchestration, tool calling, RAG
- Multi-tenancy, user accounts, billing
- Kubernetes deployment (Compose is fine; you already have k8s on your resume)

---

## Stack

| Layer | Choice | Why this one |
|---|---|---|
| Service | Python + FastAPI | Same as your HERE inference service. Play to it. |
| HTTP client | `httpx` (async) | Async matters here — you're I/O bound on provider calls. |
| Cache + rate limit | **Redis** | Closes a gap that has come up repeatedly. Real use, not resume padding. |
| Persistence | **PostgreSQL** + SQLAlchemy | Deepens the Postgres story EV Grid started. |
| Migrations | Alembic | Postgres equivalent of the Flyway work you already did. |
| Edge proxy | **Envoy** (or nginx) | Closes the proxy-stack gap on the Microsoft req. |
| Tests | pytest + **Testcontainers** | Same pattern that makes your EV Grid benchmark credible. |
| Local orchestration | Docker Compose | gateway + redis + postgres + envoy + mock providers |

Redis, Postgres, and Envoy are deliberate. Each one closes a named gap on a real JD.

---

## Architecture

```
client
  │
  ▼
Envoy ──────────── TLS termination, edge rate limit, access logs
  │
  ▼
FastAPI gateway
  ├── Router          picks provider from policy + live health
  ├── Cache           Redis lookup before any provider call
  ├── Adapters        anthropic / openai / google → normalized shape
  ├── Failover        retry w/ backoff, then next provider in policy order
  ├── Cost accounting tokens in/out × provider rate → Postgres
  └── Health tracker  rolling p50 latency + error rate per provider
  │
  ▼
Provider APIs  (real) ── or ── Mock provider service (benchmarks, CI)
```

### The mock provider service is not optional

A small FastAPI app that mimics all three providers' schemas, with injectable latency, error rate, and 429s. This is the single most important design decision in the project, for three reasons:

1. Benchmarks become deterministic and reproducible by anyone who clones the repo
2. CI runs without API keys or spend
3. You can demo failover on demand instead of waiting for a real outage

You have already built exactly this once — the Kotlin provider simulator in EV Grid. Same pattern, and you can say so.

---

## Data model

```
requests
  id, ts, route_policy, provider_attempted[], provider_served,
  cache_hit, latency_ms, tokens_in, tokens_out, cost_usd,
  status, failover_reason

provider_health          -- rolling window, can live in Redis
  provider, window_start, p50_ms, p95_ms, error_rate, rate_limited_count
```

`provider_attempted` as an array is what lets you show a failover chain in the demo rather than just asserting it works.

---

## The three artifacts that make this resume-worthy

Everything else is scaffolding. These are the deliverables.

**1. Cache benchmark.** Cold vs. warm p50 and p95 across N requests against the mock provider. Committed script, seeded workload, numbers in the README. This is your "2.1s → 95ms" for this project.

**2. Failover demo.** A script that sets the mock provider's error rate to 100% mid-run and shows requests continuing to succeed, with the recorded `provider_attempted` chain proving the path. Record it as a terminal asciicast in the README.

**3. Cost comparison table.** Same workload routed under each policy, showing actual dollar difference. Cheapest-first vs. pinned-to-one is a number nobody else applying will have.

Write all three numbers into the README with the method stated — how many runs, warmups discarded, what hardware. The method is what made your EV Grid number defensible; do the same here.

---

## Build order

Each milestone should end with something demoable. If you fall behind, stop at 4 — it is still a complete story.

**1. Skeleton (day 1–2)**
Compose file, FastAPI app, `/health`, Postgres + Alembic wired. One provider, hardcoded, working end to end.
*Done when:* `docker compose up` then one real completion returns.

**2. Adapters + normalization (day 3–4)**
Three adapters behind one interface. Mock provider service with all three schemas.
*Done when:* the same request body returns the same response shape from all three, real or mocked.

**3. Routing + failover (day 5–7)**
Policy engine, health tracker, retry with backoff, provider fallthrough.
*Done when:* the failover demo script runs and the chain is recorded in Postgres.

**4. Cache + cost accounting (day 8–10)**
Redis caching, token counting, cost table, `/stats` endpoint.
*Done when:* the cache benchmark and cost comparison produce committed numbers.

**5. Edge + hardening (day 11–13)**
Envoy in front, TLS, edge rate limiting. Testcontainers integration suite. GitHub Actions CI.
*Done when:* CI is green on a clean clone with no API keys.

**6. README (day 14)**
Architecture diagram, the three artifacts with methods, quickstart. Treat this as the deliverable, not an afterthought — it is what a recruiter actually reads.

---

## Resume bullets this produces

Drafted now so you know what you're building toward. Fill the brackets with real measurements.

> Built a multi-provider **LLM gateway** (Python, FastAPI, Redis, PostgreSQL) presenting one interface across Anthropic, OpenAI, and Google APIs, with policy-based routing, automatic failover, and per-request cost accounting

> Cut p50 latency from **[X] ms to [Y] ms** via Redis response caching and reduced workload cost **[Z]%** through cheapest-first routing, both verified by committed benchmarks running against a fault-injecting mock provider

> Fronted the service with **Envoy** for TLS termination and edge rate limiting; validated failover and rate-limit paths with a **Testcontainers** integration suite running in **GitHub Actions** CI

---

## Questions it has to survive

Build with these in mind. If you can't answer one, that part isn't finished.

- What happens when two requests miss the cache for the same prompt simultaneously?
- How do you cache-key requests that differ only in `temperature`?
- Why backoff and not immediate failover? What are the tradeoffs?
- How do you count tokens for a provider that doesn't return usage on error?
- What breaks if Redis goes down? (Correct answer: nothing — degrade to no caching.)
- Why Envoy instead of handling rate limiting in the app?

---

## Notes

Build it with Claude Code, and keep a short log of what you delegated versus wrote yourself. That log turns your `AI-Assisted Development` skills line into a specific interview answer instead of a claim.

Public repo from commit one. Real README from the start. The repo being visibly built over two weeks is itself evidence.
