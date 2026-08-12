import httpx

from app.adapters.base import Adapter, missing
from app.schemas import ChatRequest, Provider, ProviderCompletion


class OpenAIAdapter(Adapter):
    """Chat Completions: system prompt is a message, content comes back under choices."""

    provider = Provider.openai

    async def complete(
        self, client: httpx.AsyncClient, req: ChatRequest, model: str
    ) -> ProviderCompletion:
        messages: list[dict] = []
        if req.system:
            messages.append({"role": "system", "content": req.system})
        messages += [{"role": m.role, "content": m.content} for m in req.messages]

        headers = {"content-type": "application/json"}
        if self.api_key:
            # Only when we have one: httpx rejects "Bearer " (trailing space) as an
            # illegal header value, which surfaces as a transport error and looks
            # exactly like the provider being down.
            headers["authorization"] = f"Bearer {self.api_key}"

        data = await self._post(
            client,
            f"{self.base_url}/v1/chat/completions",
            json={
                "model": model,
                "messages": messages,
                "max_tokens": req.max_tokens,
                "temperature": req.temperature,
            },
            headers=headers,
        )

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise missing("choices")
        text = (choices[0].get("message") or {}).get("content") or ""

        usage = data.get("usage") or {}
        return ProviderCompletion(
            content=text,
            model=data.get("model", model),
            tokens_in=int(usage.get("prompt_tokens", 0)),
            tokens_out=int(usage.get("completion_tokens", 0)),
            usage_reported="prompt_tokens" in usage,
        )
