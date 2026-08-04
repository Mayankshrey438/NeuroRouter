"""
Lets an admin manually take a provider out of rotation (distinct from the
circuit breaker, which trips automatically on real failures). Backed by a
simple Redis set so the router can cheaply check membership per request.
"""
from redis.asyncio import Redis

DISABLED_SET_KEY = "providers:disabled"


class ProviderControl:
    def __init__(self, redis: Redis):
        self.redis = redis

    async def disable(self, provider: str):
        await self.redis.sadd(DISABLED_SET_KEY, provider)

    async def enable(self, provider: str):
        await self.redis.srem(DISABLED_SET_KEY, provider)

    async def disabled_set(self) -> set:
        members = await self.redis.smembers(DISABLED_SET_KEY)
        return set(members)
