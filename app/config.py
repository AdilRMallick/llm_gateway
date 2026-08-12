from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GATEWAY_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://gateway:gateway@localhost:5432/gateway"
    redis_url: str = "redis://localhost:6379/0"

    # When a base URL is set the adapter talks to it instead of the real provider.
    # The mock provider service sets all three in local + CI runs.
    anthropic_base_url: str | None = None
    openai_base_url: str | None = None
    google_base_url: str | None = None

    anthropic_api_key: str = ""
    openai_api_key: str = ""
    google_api_key: str = ""

    request_timeout_s: float = 30.0
    connect_timeout_s: float = 5.0

    # Failover behaviour
    max_attempts_per_provider: int = 2
    backoff_base_s: float = 0.05
    backoff_max_s: float = 2.0

    # Cache
    cache_enabled: bool = True
    cache_ttl_s: int = 3600
    # Requests above this temperature are not cached: the provider is being asked
    # for a sampled answer, so replaying one recorded sample is the wrong semantics.
    cache_max_temperature: float = 0.0
    # Single-flight: how long a cache-miss holder may hold the lock, and how long
    # followers wait for its result before giving up and calling the provider.
    cache_lock_ttl_s: int = 30
    cache_lock_wait_s: float = 5.0
    cache_lock_poll_s: float = 0.02

    # Health tracker rolling window
    health_window_s: int = 60
    health_max_samples: int = 500

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
