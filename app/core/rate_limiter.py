"""
Sliding-window rate limiter using a Redis sorted set per API key.

Algorithm:
  - Each request adds a member (unique id) to a ZSET scored by timestamp.
  - We trim anything older than `window_seconds`.
  - If the remaining count >= limit, the request is rejected.

This is more accurate than a fixed-window counter (no boundary burst
problem) while still being O(log N) and atomic via a Lua-less pipeline
(we accept a tiny race window, acceptable for a gateway rate limiter).
"""
import time
import uuid

from redis.asyncio import Redis


class RateLimitExceeded(Exception):
    def __init__(self, retry_after_seconds: float):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"rate limit exceeded, retry after {retry_after_seconds:.1f}s")


class RateLimiter:
    def __init__(self, redis: Redis, limit: int, window_seconds: int):
        self.redis = redis
        self.limit = limit
        self.window_seconds = window_seconds

    async def check(self, key: str):
        redis_key = f"rl:{key}"
        now = time.time()
        window_start = now - self.window_seconds

        pipe = self.redis.pipeline()
        pipe.zremrangebyscore(redis_key, 0, window_start)
        pipe.zcard(redis_key)
        _, current_count = await pipe.execute()

        if current_count >= self.limit:
            oldest = await self.redis.zrange(redis_key, 0, 0, withscores=True)
            retry_after = self.window_seconds
            if oldest:
                retry_after = max(0.0, self.window_seconds - (now - oldest[0][1]))
            raise RateLimitExceeded(retry_after)

        pipe = self.redis.pipeline()
        pipe.zadd(redis_key, {str(uuid.uuid4()): now})
        pipe.expire(redis_key, self.window_seconds)
        await pipe.execute()

    async def remaining(self, key: str) -> int:
        redis_key = f"rl:{key}"
        now = time.time()
        window_start = now - self.window_seconds
        await self.redis.zremrangebyscore(redis_key, 0, window_start)
        count = await self.redis.zcard(redis_key)
        return max(0, self.limit - count)
