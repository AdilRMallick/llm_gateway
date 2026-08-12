"""Policy engine: turns a request into an ordered list of (provider, model) to try.

`pinned` deliberately returns exactly one candidate. A pin that quietly falls
through to another provider is not a pin — and the cost comparison in the README
only means something if pinned-to-one really did stay on one provider.
"""

from dataclasses import dataclass

from app.health import ProviderHealth
from app.pricing import DEFAULT_MODEL, estimate_cost_usd, estimate_tokens
from app.schemas import ChatRequest, Provider, RoutePolicy


@dataclass(frozen=True)
class Candidate:
    provider: Provider
    model: str
    est_cost_usd: float
    health_score: float


def resolve_model(req: ChatRequest, provider: Provider) -> str:
    return req.models.get(provider) or DEFAULT_MODEL[provider]


def prompt_tokens_estimate(req: ChatRequest) -> int:
    text = (req.system or "") + "".join(m.content for m in req.messages)
    return estimate_tokens(text)


class Router:
    def __init__(self, health: ProviderHealth, providers: list[Provider] | None = None):
        self.health = health
        self.providers = providers or list(Provider)

    async def plan(self, req: ChatRequest) -> list[Candidate]:
        tokens_in = prompt_tokens_estimate(req)

        if req.policy is RoutePolicy.pinned:
            if req.provider is None:
                raise ValueError("policy 'pinned' requires a provider")
            targets = [req.provider]
        else:
            targets = self.providers

        candidates = []
        for p in targets:
            model = resolve_model(req, p)
            candidates.append(
                Candidate(
                    provider=p,
                    model=model,
                    est_cost_usd=estimate_cost_usd(p, model, tokens_in, req.max_tokens),
                    health_score=await self.health.score(p),
                )
            )

        if req.policy is RoutePolicy.cheapest:
            # Cost first, health as the tiebreak: two providers at the same price
            # should be split by which one is currently answering.
            candidates.sort(key=lambda c: (c.est_cost_usd, c.health_score))
        elif req.policy is RoutePolicy.fastest:
            candidates.sort(key=lambda c: (c.health_score, c.est_cost_usd))

        return candidates
