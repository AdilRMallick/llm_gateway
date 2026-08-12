import httpx

from app.adapters.base import Adapter, missing
from app.schemas import ChatRequest, Provider, ProviderCompletion


class GoogleAdapter(Adapter):
    """Gemini generateContent: model name is in the path, roles are user/model,
    text lives in parts, and the API key travels as a query param."""

    provider = Provider.google

    async def complete(
        self, client: httpx.AsyncClient, req: ChatRequest, model: str
    ) -> ProviderCompletion:
        contents = [
            {
                "role": "user" if m.role == "user" else "model",
                "parts": [{"text": m.content}],
            }
            for m in req.messages
        ]
        body: dict = {
            "contents": contents,
            "generationConfig": {
                "maxOutputTokens": req.max_tokens,
                "temperature": req.temperature,
            },
        }
        if req.system:
            body["systemInstruction"] = {"parts": [{"text": req.system}]}

        data = await self._post(
            client,
            f"{self.base_url}/v1beta/models/{model}:generateContent",
            json=body,
            headers={"content-type": "application/json"},
            params={"key": self.api_key} if self.api_key else None,
        )

        candidates = data.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise missing("candidates")
        parts = ((candidates[0].get("content") or {}).get("parts")) or []
        text = "".join(p.get("text", "") for p in parts)

        usage = data.get("usageMetadata") or {}
        # Thinking tokens are billed as output — Google's pricing page prices
        # output "including thinking tokens" — but they are reported in their own
        # field, not folded into candidatesTokenCount. Counting only candidates
        # under-reports cost by more than 10x on a reasoning model: an 8-token
        # prompt to gemini-3.6-flash came back with candidatesTokenCount=2 and
        # thoughtsTokenCount=24.
        tokens_out = int(usage.get("candidatesTokenCount", 0)) + int(
            usage.get("thoughtsTokenCount", 0)
        )
        return ProviderCompletion(
            content=text,
            model=data.get("modelVersion", model),
            tokens_in=int(usage.get("promptTokenCount", 0)),
            tokens_out=tokens_out,
            usage_reported="promptTokenCount" in usage,
        )
