"""Fault-injecting stand-in for all three providers.

Speaks each provider's real request/response schema, with per-provider injectable
latency, error rate, 429 rate, and usage-block suppression. This is what makes the
benchmarks in the README reproducible on a clean clone with no API keys and no
spend, and what lets the failover demo cause an outage on purpose instead of
waiting for one.

Control plane (not part of any provider's API):
  GET  /_control                  -> current knobs for all three
  POST /_control/{provider}       -> set knobs  {"error_rate": 1.0, ...}
  POST /_control/reset            -> back to defaults
"""

import asyncio
import hashlib
import json
import random

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

PROVIDERS = ("anthropic", "openai", "google")


class Knobs(BaseModel):
    latency_ms: float = Field(default=40.0, ge=0)
    latency_jitter_ms: float = Field(default=0.0, ge=0)
    error_rate: float = Field(default=0.0, ge=0.0, le=1.0)  # -> 503
    rate_limit_rate: float = Field(default=0.0, ge=0.0, le=1.0)  # -> 429
    # -> 400, i.e. an error retrying cannot fix
    bad_request_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    retry_after_s: float | None = None
    usage_reported: bool = True
    seed: int = 1337


class KnobPatch(BaseModel):
    latency_ms: float | None = Field(default=None, ge=0)
    latency_jitter_ms: float | None = Field(default=None, ge=0)
    error_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    rate_limit_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    bad_request_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    retry_after_s: float | None = None
    usage_reported: bool | None = None
    seed: int | None = None


def _zero_counts() -> dict[str, int]:
    """Call counts per provider. Tests assert on these — notably that ten
    concurrent identical requests produce one upstream call, not ten."""
    return {"calls": 0, "ok": 0, "429": 0, "503": 0, "400": 0}


app = FastAPI(title="Mock LLM Providers", version="0.1.0")

STATE: dict[str, Knobs] = {p: Knobs() for p in PROVIDERS}
RNG: dict[str, random.Random] = {p: random.Random(STATE[p].seed) for p in PROVIDERS}
COUNTS: dict[str, dict[str, int]] = {p: _zero_counts() for p in PROVIDERS}


def _snapshot() -> dict:
    return {p: {**STATE[p].model_dump(), "counts": dict(COUNTS[p])} for p in PROVIDERS}


# --------------------------------------------------------------------------
# control plane
# --------------------------------------------------------------------------
@app.get("/_control")
async def get_control() -> dict:
    return _snapshot()


@app.post("/_control/reset")
async def reset_control() -> dict:
    for p in PROVIDERS:
        STATE[p] = Knobs()
        RNG[p] = random.Random(STATE[p].seed)
        COUNTS[p] = _zero_counts()
    return _snapshot()


@app.post("/_control/{provider}")
async def set_control(provider: str, patch: KnobPatch) -> dict:
    if provider not in PROVIDERS:
        raise HTTPException(status_code=404, detail=f"unknown provider {provider}")
    current = STATE[provider].model_dump()
    current.update({k: v for k, v in patch.model_dump().items() if v is not None})
    STATE[provider] = Knobs(**current)
    if patch.seed is not None:
        RNG[provider] = random.Random(patch.seed)
    return STATE[provider].model_dump()


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


# --------------------------------------------------------------------------
# shared behaviour
# --------------------------------------------------------------------------
async def gate(provider: str) -> Response | None:
    """Apply the injected latency, then maybe fail. Returns a Response to send
    instead of the completion, or None to proceed."""
    k = STATE[provider]
    rng = RNG[provider]
    COUNTS[provider]["calls"] += 1

    delay = k.latency_ms + (
        rng.uniform(-k.latency_jitter_ms, k.latency_jitter_ms) if k.latency_jitter_ms else 0.0
    )
    if delay > 0:
        await asyncio.sleep(max(0.0, delay) / 1000.0)

    if k.rate_limit_rate and rng.random() < k.rate_limit_rate:
        COUNTS[provider]["429"] += 1
        headers = {"retry-after": str(k.retry_after_s)} if k.retry_after_s else {}
        return _error(429, "rate_limit_error", f"{provider} is rate limiting", headers)
    if k.error_rate and rng.random() < k.error_rate:
        COUNTS[provider]["503"] += 1
        return _error(503, "overloaded_error", f"{provider} is overloaded")
    if k.bad_request_rate and rng.random() < k.bad_request_rate:
        COUNTS[provider]["400"] += 1
        return _error(400, "invalid_request_error", f"{provider} rejected the request")
    COUNTS[provider]["ok"] += 1
    return None


def _error(status: int, kind: str, message: str, headers: dict | None = None) -> Response:
    """Roughly the error envelope all three providers use."""
    return Response(
        status_code=status,
        headers=headers,
        media_type="application/json",
        content=json.dumps({"error": {"type": kind, "message": message}}),
    )


def tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def answer(provider: str, model: str, prompt: str) -> str:
    """Deterministic for a given prompt, so a cache hit is verifiable by content
    and two runs of the benchmark produce the same bytes."""
    digest = hashlib.sha256(f"{provider}|{model}|{prompt}".encode()).hexdigest()[:12]
    head = prompt.strip().replace("\n", " ")[:120]
    return (
        f"[{provider}/{model}] {digest} — responding to: {head}. "
        "This is a deterministic mock completion used for gateway benchmarks."
    )


# --------------------------------------------------------------------------
# anthropic: POST /anthropic/v1/messages
# --------------------------------------------------------------------------
@app.post("/anthropic/v1/messages")
async def anthropic_messages(request: Request) -> Response:
    if (blocked := await gate("anthropic")) is not None:
        return blocked
    body = await request.json()
    model = body.get("model", "claude-haiku-4-5")
    prompt = (body.get("system") or "") + " ".join(
        m.get("content", "") for m in body.get("messages", [])
    )
    text = answer("anthropic", model, prompt)

    payload: dict = {
        "id": "msg_" + hashlib.md5(prompt.encode()).hexdigest()[:16],
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
    }
    if STATE["anthropic"].usage_reported:
        payload["usage"] = {"input_tokens": tokens(prompt), "output_tokens": tokens(text)}
    return _json(payload)


# --------------------------------------------------------------------------
# openai: POST /openai/v1/chat/completions
# --------------------------------------------------------------------------
@app.post("/openai/v1/chat/completions")
async def openai_chat(request: Request) -> Response:
    if (blocked := await gate("openai")) is not None:
        return blocked
    body = await request.json()
    model = body.get("model", "gpt-4o-mini")
    prompt = " ".join(m.get("content", "") for m in body.get("messages", []))
    text = answer("openai", model, prompt)

    payload: dict = {
        "id": "chatcmpl_" + hashlib.md5(prompt.encode()).hexdigest()[:16],
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
    }
    if STATE["openai"].usage_reported:
        payload["usage"] = {
            "prompt_tokens": tokens(prompt),
            "completion_tokens": tokens(text),
            "total_tokens": tokens(prompt) + tokens(text),
        }
    return _json(payload)


# --------------------------------------------------------------------------
# google: POST /google/v1beta/models/{model}:generateContent
# --------------------------------------------------------------------------
@app.post("/google/v1beta/models/{model_action}")
async def google_generate(model_action: str, request: Request) -> Response:
    if (blocked := await gate("google")) is not None:
        return blocked
    model = model_action.split(":", 1)[0]
    body = await request.json()
    sys_parts = ((body.get("systemInstruction") or {}).get("parts")) or []
    prompt = " ".join(p.get("text", "") for p in sys_parts) + " ".join(
        p.get("text", "") for c in body.get("contents", []) for p in (c.get("parts") or [])
    )
    text = answer("google", model, prompt)

    payload: dict = {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": text}]},
                "finishReason": "STOP",
            }
        ],
        "modelVersion": model,
    }
    if STATE["google"].usage_reported:
        payload["usageMetadata"] = {
            "promptTokenCount": tokens(prompt),
            "candidatesTokenCount": tokens(text),
            "totalTokenCount": tokens(prompt) + tokens(text),
        }
    return _json(payload)


def _json(payload: dict) -> Response:
    return Response(content=json.dumps(payload), media_type="application/json")
