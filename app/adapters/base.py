from abc import ABC, abstractmethod
from typing import Any

import httpx

from app.errors import ErrorKind, ProviderError
from app.schemas import ChatRequest, Provider, ProviderCompletion


class Adapter(ABC):
    """One provider's request/response schema, hidden behind one method.

    Everything above this layer — router, failover, cache, accounting — deals only
    in ChatRequest / ProviderCompletion and never learns a provider's wire format.
    """

    provider: Provider

    def __init__(self, base_url: str, api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    @abstractmethod
    async def complete(
        self, client: httpx.AsyncClient, req: ChatRequest, model: str
    ) -> ProviderCompletion: ...

    async def _post(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            resp = await client.post(url, json=json, headers=headers, params=params)
        except httpx.TimeoutException as e:
            raise ProviderError(ErrorKind.timeout, str(e) or "request timed out") from e
        except httpx.LocalProtocolError as e:
            # We built a malformed request. It is a subclass of TransportError, so
            # without this branch our own bugs get reported as the provider being
            # unreachable — and then retried, which cannot possibly help.
            raise ProviderError(ErrorKind.client_error, f"malformed request: {e}") from e
        except httpx.TransportError as e:
            raise ProviderError(ErrorKind.connection, str(e) or "connection failed") from e

        if resp.status_code == 429:
            raise ProviderError(
                ErrorKind.rate_limited,
                _snippet(resp),
                http_status=429,
                retry_after_s=_retry_after(resp),
            )
        if resp.status_code >= 500:
            raise ProviderError(
                ErrorKind.server_error, _snippet(resp), http_status=resp.status_code
            )
        if resp.status_code >= 400:
            raise ProviderError(
                ErrorKind.client_error, _snippet(resp), http_status=resp.status_code
            )

        try:
            return resp.json()
        except ValueError as e:
            raise ProviderError(
                ErrorKind.bad_response, "response was not JSON", http_status=resp.status_code
            ) from e


def _snippet(resp: httpx.Response, limit: int = 200) -> str:
    return resp.text[:limit].replace("\n", " ").strip() or f"HTTP {resp.status_code}"


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("retry-after")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def missing(field: str) -> ProviderError:
    return ProviderError(ErrorKind.bad_response, f"missing {field} in provider response")
