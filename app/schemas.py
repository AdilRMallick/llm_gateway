"""The provider-agnostic wire shape. Adapters translate to and from this."""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class Provider(StrEnum):
    anthropic = "anthropic"
    openai = "openai"
    google = "google"


class RoutePolicy(StrEnum):
    pinned = "pinned"
    cheapest = "cheapest"
    fastest = "fastest"


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    messages: list[Message] = Field(min_length=1)
    system: str | None = None
    max_tokens: int = Field(default=512, ge=1, le=8192)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)

    policy: RoutePolicy = RoutePolicy.cheapest
    # Required when policy is "pinned"; ignored otherwise.
    provider: Provider | None = None
    # Optional per-provider model override, e.g. {"openai": "gpt-4o-mini"}.
    models: dict[Provider, str] = Field(default_factory=dict)


class Usage(BaseModel):
    tokens_in: int
    tokens_out: int
    cost_usd: float


class Attempt(BaseModel):
    provider: Provider
    model: str
    status: Literal["ok", "error"]
    latency_ms: int
    error_kind: str | None = None
    http_status: int | None = None


class ChatResponse(BaseModel):
    id: str
    content: str
    provider: Provider
    model: str
    cache_hit: bool
    latency_ms: int
    usage: Usage
    attempts: list[Attempt]
    policy: RoutePolicy


class ProviderCompletion(BaseModel):
    """What an adapter returns: normalized text plus whatever usage the provider gave us."""

    content: str
    model: str
    tokens_in: int
    tokens_out: int
    # True when the provider reported usage; False when we estimated it locally.
    usage_reported: bool = True
