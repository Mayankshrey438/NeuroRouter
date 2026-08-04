"""
Runtime-editable configuration.

Certain settings (rate limit, cache TTL/threshold, circuit breaker
thresholds) are useful to tune live from the Settings screen without a
redeploy. Overrides are stored in a Redis hash (`config:overrides`) and
merged over the .env-sourced defaults. Reads are cached in-process for a
couple of seconds to avoid hitting Redis on every single request for
values that change rarely.
"""
import time
from typing import Any, Dict

from redis.asyncio import Redis

from app.config import Settings

CONFIG_KEY = "config:overrides"

TUNABLE_FIELDS = {
    "rate_limit_requests": int,
    "rate_limit_window_seconds": int,
    "cache_ttl_seconds": int,
    "semantic_cache_threshold": float,
    "semantic_cache_max_entries": int,
    "circuit_failure_threshold": int,
    "circuit_recovery_seconds": int,
}


class RuntimeConfig:
    def __init__(self, redis: Redis, settings: Settings, cache_seconds: float = 2.0):
        self.redis = redis
        self.settings = settings
        self.cache_seconds = cache_seconds
        self._cached: Dict[str, Any] = {}
        self._cached_at = 0.0

    async def get(self) -> Dict[str, Any]:
        now = time.time()
        if self._cached and (now - self._cached_at) < self.cache_seconds:
            return self._cached

        defaults = {field: getattr(self.settings, field) for field in TUNABLE_FIELDS}
        overrides_raw = await self.redis.hgetall(CONFIG_KEY)

        merged = dict(defaults)
        for field, caster in TUNABLE_FIELDS.items():
            if field in overrides_raw:
                try:
                    merged[field] = caster(overrides_raw[field])
                except (TypeError, ValueError):
                    pass

        self._cached = merged
        self._cached_at = now
        return merged

    async def update(self, values: Dict[str, Any]) -> Dict[str, Any]:
        to_store = {}
        for field, value in values.items():
            if field not in TUNABLE_FIELDS or value is None:
                continue
            caster = TUNABLE_FIELDS[field]
            to_store[field] = str(caster(value))

        if to_store:
            await self.redis.hset(CONFIG_KEY, mapping=to_store)
        self._cached_at = 0.0  # invalidate cache
        return await self.get()

    async def reset(self) -> Dict[str, Any]:
        await self.redis.delete(CONFIG_KEY)
        self._cached_at = 0.0
        return await self.get()
