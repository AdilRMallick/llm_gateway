from app.adapters.anthropic import AnthropicAdapter
from app.adapters.base import Adapter
from app.adapters.google import GoogleAdapter
from app.adapters.openai import OpenAIAdapter
from app.config import Settings
from app.schemas import Provider

DEFAULT_BASE_URL: dict[Provider, str] = {
    Provider.anthropic: "https://api.anthropic.com",
    Provider.openai: "https://api.openai.com",
    Provider.google: "https://generativelanguage.googleapis.com",
}


def build_adapters(settings: Settings) -> dict[Provider, Adapter]:
    """Base URL from config when set (mock provider), real endpoint otherwise."""
    return {
        Provider.anthropic: AnthropicAdapter(
            settings.anthropic_base_url or DEFAULT_BASE_URL[Provider.anthropic],
            settings.anthropic_api_key,
        ),
        Provider.openai: OpenAIAdapter(
            settings.openai_base_url or DEFAULT_BASE_URL[Provider.openai],
            settings.openai_api_key,
        ),
        Provider.google: GoogleAdapter(
            settings.google_base_url or DEFAULT_BASE_URL[Provider.google],
            settings.google_api_key,
        ),
    }


__all__ = ["Adapter", "AnthropicAdapter", "OpenAIAdapter", "GoogleAdapter", "build_adapters"]
