"""Adapter unit tests: each provider's real wire shape in, one normalized shape out.

No containers — these are pure translation tests against recorded response bodies,
so a schema change in an adapter fails here loudly instead of in an integration
test that says only "502".
"""

import httpx
import pytest

from app.adapters import AnthropicAdapter, GoogleAdapter, OpenAIAdapter
from app.errors import ErrorKind, ProviderError
from app.schemas import ChatRequest, Message

REQ = ChatRequest(
    messages=[Message(role="user", content="why is the sky blue")],
    system="be brief",
    max_tokens=100,
    temperature=0.0,
)

ANTHROPIC_BODY = {
    "id": "msg_01",
    "type": "message",
    "role": "assistant",
    "model": "claude-haiku-4-5",
    "content": [
        {"type": "text", "text": "Rayleigh "},
        {"type": "thinking", "thinking": "ignored"},
        {"type": "text", "text": "scattering."},
    ],
    "usage": {"input_tokens": 11, "output_tokens": 4},
}

OPENAI_BODY = {
    "id": "chatcmpl-01",
    "object": "chat.completion",
    "model": "gpt-4o-mini",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "Rayleigh scattering."}}],
    "usage": {"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15},
}

GOOGLE_BODY = {
    "candidates": [
        {"content": {"role": "model", "parts": [{"text": "Rayleigh "}, {"text": "scattering."}]}}
    ],
    "modelVersion": "gemini-2.0-flash",
    "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 4},
}


def client_returning(status: int, json_body: dict | None = None, headers=None, text=None):
    def handler(request: httpx.Request) -> httpx.Response:
        handler.request = request
        if text is not None:
            return httpx.Response(status, text=text, headers=headers)
        return httpx.Response(status, json=json_body, headers=headers)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), handler


@pytest.mark.parametrize(
    ("adapter_cls", "body", "model"),
    [
        (AnthropicAdapter, ANTHROPIC_BODY, "claude-haiku-4-5"),
        (OpenAIAdapter, OPENAI_BODY, "gpt-4o-mini"),
        (GoogleAdapter, GOOGLE_BODY, "gemini-2.0-flash"),
    ],
)
async def test_three_schemas_normalize_to_one(adapter_cls, body, model):
    adapter = adapter_cls("http://p")
    client, _ = client_returning(200, body)
    async with client:
        out = await adapter.complete(client, REQ, model)

    assert out.content == "Rayleigh scattering."
    assert out.model == model
    assert (out.tokens_in, out.tokens_out) == (11, 4)
    assert out.usage_reported is True


async def test_anthropic_sends_system_as_top_level_field():
    adapter = AnthropicAdapter("http://p", api_key="k")
    client, handler = client_returning(200, ANTHROPIC_BODY)
    async with client:
        await adapter.complete(client, REQ, "claude-haiku-4-5")

    import json

    sent = json.loads(handler.request.content)
    assert sent["system"] == "be brief"
    assert [m["role"] for m in sent["messages"]] == ["user"]
    assert handler.request.headers["x-api-key"] == "k"
    assert handler.request.headers["anthropic-version"]


async def test_openai_sends_system_as_a_message():
    adapter = OpenAIAdapter("http://p")
    client, handler = client_returning(200, OPENAI_BODY)
    async with client:
        await adapter.complete(client, REQ, "gpt-4o-mini")

    import json

    sent = json.loads(handler.request.content)
    assert sent["messages"][0] == {"role": "system", "content": "be brief"}
    # No key configured: the header must be absent, not empty. httpx rejects
    # "Bearer " as an illegal header value and it surfaces as a transport error.
    assert "authorization" not in handler.request.headers


async def test_google_puts_model_in_path_and_key_in_query():
    adapter = GoogleAdapter("http://p", api_key="k")
    client, handler = client_returning(200, GOOGLE_BODY)
    async with client:
        await adapter.complete(client, REQ, "gemini-2.0-flash")

    assert handler.request.url.path.endswith("/v1beta/models/gemini-2.0-flash:generateContent")
    assert handler.request.url.params["key"] == "k"

    import json

    sent = json.loads(handler.request.content)
    assert sent["systemInstruction"]["parts"][0]["text"] == "be brief"
    assert sent["contents"][0]["role"] == "user"


@pytest.mark.parametrize(
    ("status", "kind", "retryable"),
    [
        (429, ErrorKind.rate_limited, True),
        (500, ErrorKind.server_error, True),
        (503, ErrorKind.server_error, True),
        (400, ErrorKind.client_error, False),
        (401, ErrorKind.client_error, False),
    ],
)
async def test_http_status_maps_to_error_kind(status, kind, retryable):
    adapter = OpenAIAdapter("http://p")
    client, _ = client_returning(status, {"error": "nope"})
    async with client:
        with pytest.raises(ProviderError) as exc:
            await adapter.complete(client, REQ, "gpt-4o-mini")

    assert exc.value.kind is kind
    assert exc.value.retryable is retryable
    assert exc.value.http_status == status


async def test_retry_after_header_is_parsed():
    adapter = OpenAIAdapter("http://p")
    client, _ = client_returning(429, {"error": "slow down"}, headers={"retry-after": "2"})
    async with client:
        with pytest.raises(ProviderError) as exc:
            await adapter.complete(client, REQ, "gpt-4o-mini")

    assert exc.value.retry_after_s == 2.0


async def test_non_json_200_is_a_bad_response_not_a_crash():
    adapter = OpenAIAdapter("http://p")
    client, _ = client_returning(200, text="<html>gateway timeout page</html>")
    async with client:
        with pytest.raises(ProviderError) as exc:
            await adapter.complete(client, REQ, "gpt-4o-mini")

    assert exc.value.kind is ErrorKind.bad_response
    assert exc.value.retryable is True


async def test_missing_usage_block_is_flagged_not_guessed_at_adapter_level():
    body = {k: v for k, v in OPENAI_BODY.items() if k != "usage"}
    adapter = OpenAIAdapter("http://p")
    client, _ = client_returning(200, body)
    async with client:
        out = await adapter.complete(client, REQ, "gpt-4o-mini")

    assert out.usage_reported is False
    assert (out.tokens_in, out.tokens_out) == (0, 0)


async def test_timeout_maps_to_timeout_kind():
    def handler(request):
        raise httpx.ReadTimeout("too slow", request=request)

    adapter = OpenAIAdapter("http://p")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError) as exc:
            await adapter.complete(client, REQ, "gpt-4o-mini")

    assert exc.value.kind is ErrorKind.timeout
    assert exc.value.retryable is True
