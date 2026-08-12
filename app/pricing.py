"""Per-model rates and the token estimator used when a provider gives us no usage.

Rates are USD per 1M tokens, taken from each provider's public pricing page.
They are data, not logic: edit this table when prices move. `PRICING_AS_OF` is
printed by /stats so a cost number always carries the rate card it was computed
against.
"""

from dataclasses import dataclass

from app.schemas import Provider

PRICING_AS_OF = "2026-08"


@dataclass(frozen=True)
class Rate:
    input_per_mtok: float
    output_per_mtok: float

    def cost_usd(self, tokens_in: int, tokens_out: int) -> float:
        return (tokens_in * self.input_per_mtok + tokens_out * self.output_per_mtok) / 1_000_000


RATES: dict[Provider, dict[str, Rate]] = {
    Provider.anthropic: {
        "claude-haiku-4-5": Rate(1.00, 5.00),
        "claude-sonnet-4-5": Rate(3.00, 15.00),
    },
    Provider.openai: {
        "gpt-4o-mini": Rate(0.15, 0.60),
        "gpt-4o": Rate(2.50, 10.00),
    },
    # Verified against ai.google.dev/gemini-api/docs/pricing, paid tier, 2026-08.
    # Google's output price is quoted "including thinking tokens", which is why
    # the adapter adds thoughtsTokenCount into tokens_out.
    Provider.google: {
        "gemini-3.5-flash-lite": Rate(0.30, 2.50),
        "gemini-3.1-flash-lite": Rate(0.25, 1.50),
        "gemini-3.5-flash": Rate(1.50, 7.50),
        "gemini-3.6-flash": Rate(1.50, 7.50),
    },
}

DEFAULT_MODEL: dict[Provider, str] = {
    Provider.anthropic: "claude-haiku-4-5",
    Provider.openai: "gpt-4o-mini",
    Provider.google: "gemini-3.5-flash-lite",
}

# Used only when a model is not in the table, so an unknown model never crashes a
# request — it just costs nothing and is visibly wrong in /stats.
UNKNOWN_RATE = Rate(0.0, 0.0)


def rate_for(provider: Provider, model: str) -> Rate:
    return RATES.get(provider, {}).get(model, UNKNOWN_RATE)


def cost_usd(provider: Provider, model: str, tokens_in: int, tokens_out: int) -> float:
    return rate_for(provider, model).cost_usd(tokens_in, tokens_out)


def estimate_cost_usd(provider: Provider, model: str, tokens_in: int, max_tokens: int) -> float:
    """Pre-flight cost estimate for cheapest-first routing.

    Worst case on output: we assume the model fills max_tokens. That makes the
    ordering stable across requests instead of depending on how chatty a model
    happened to be last time.
    """
    return rate_for(provider, model).cost_usd(tokens_in, max_tokens)


def estimate_tokens(text: str) -> int:
    """~4 chars per token. Deliberately crude.

    This is only used when a provider returns an error mid-stream or omits usage,
    which is exactly the case where no exact count exists. Rows filled this way are
    flagged `usage_estimated=true` so aggregate cost can be reported with and
    without them.
    """
    return max(1, (len(text) + 3) // 4)
