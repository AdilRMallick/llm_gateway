import httpx

from app.adapters.base import Adapter, missing
from app.schemas import ChatRequest, Provider, ProviderCompletion

ANTHROPIC_VERSION = "2023-06-01"


class AnthropicAdapter(Adapter):
    """Messages API: system prompt is a top-level field, content comes back as blocks."""

    provider = Provider.anthropic

    async def complete(
        self, client: httpx.AsyncClient, req: ChatRequest, model: str
    ) -> ProviderCompletion:
        body: dict = {
            "model": model,
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
            "messages": [{"role": m.role, "content": m.content} for m in req.messages],
        }
        if req.system:
            body["system"] = req.system

        headers = {
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key

        data = await self._post(client, f"{self.base_url}/v1/messages", json=body, headers=headers)

        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise missing("content")
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")

        usage = data.get("usage") or {}
        return ProviderCompletion(
            content=text,
            model=data.get("model", model),
            tokens_in=int(usage.get("input_tokens", 0)),
            tokens_out=int(usage.get("output_tokens", 0)),
            usage_reported="input_tokens" in usage,
        )
