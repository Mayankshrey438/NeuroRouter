"""
Centralised configuration for NeuroRouter.

Everything is loaded from environment variables (see .env.example).
Using pydantic-settings means we get validation + sane defaults for free,
and the rest of the app never touches os.environ directly.
"""
from functools import lru_cache
from typing import List, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Auth
    neurorouter_api_keys: str = "dev-key-123"
    neurorouter_admin_token: str = "admin-dev-token"

    # Providers
    groq_api_key: Optional[str] = None
    openai_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None

    # Cache
    cache_ttl_seconds: int = 3600
    semantic_cache_threshold: float = 0.92
    semantic_cache_max_entries: int = 2000

    # Rate limiting
    rate_limit_requests: int = 60
    rate_limit_window_seconds: int = 60

    # Circuit breaker
    circuit_failure_threshold: int = 5
    circuit_recovery_seconds: int = 30

    @property
    def api_keys(self) -> List[str]:
        if not self.neurorouter_api_keys.strip():
            return []
        return [k.strip() for k in self.neurorouter_api_keys.split(",") if k.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
