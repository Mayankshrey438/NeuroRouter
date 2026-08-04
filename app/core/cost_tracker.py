"""
Running totals for the /v1/stats dashboard.

Tracks: total requests, cache hits (by type), tokens, estimated spend, and
estimated spend *saved* by caching + routing to smaller models instead of
always calling the largest model. All counters are simple Redis
INCR/INCRBYFLOAT operations - cheap, atomic, and shared across workers.
"""
from redis.asyncio import Redis

KEYS = {
    "total_requests": "stats:total_requests",
    "cache_hits_exact": "stats:cache_hits_exact",
    "cache_hits_semantic": "stats:cache_hits_semantic",
    "cache_misses": "stats:cache_misses",
    "total_prompt_tokens": "stats:total_prompt_tokens",
    "total_completion_tokens": "stats:total_completion_tokens",
    "total_cost_usd": "stats:total_cost_usd",
    "estimated_savings_usd": "stats:estimated_savings_usd",
    "fallbacks_triggered": "stats:fallbacks_triggered",
    "circuit_trips": "stats:circuit_trips",
}

# What the *largest* model would have cost, for savings comparison purposes.
BASELINE_RATE_PER_1M = {"input": 0.59, "output": 0.79}


class CostTracker:
    def __init__(self, redis: Redis):
        self.redis = redis

    async def record_request(
        self,
        cache_hit: bool,
        cache_type: str | None,
        prompt_tokens: int,
        completion_tokens: int,
        actual_cost_usd: float,
        fallback_used: bool = False,
    ):
        pipe = self.redis.pipeline()
        pipe.incr(KEYS["total_requests"])

        if cache_hit:
            if cache_type == "exact":
                pipe.incr(KEYS["cache_hits_exact"])
            else:
                pipe.incr(KEYS["cache_hits_semantic"])
        else:
            pipe.incr(KEYS["cache_misses"])

        pipe.incrby(KEYS["total_prompt_tokens"], prompt_tokens)
        pipe.incrby(KEYS["total_completion_tokens"], completion_tokens)
        pipe.incrbyfloat(KEYS["total_cost_usd"], actual_cost_usd)

        # Savings: if this was a cache hit, we saved what the baseline model
        # would have cost for an equivalent call. If it was a miss but we
        # used a *cheaper* model than baseline, the difference also counts.
        baseline_cost = (
            (prompt_tokens / 1_000_000) * BASELINE_RATE_PER_1M["input"]
            + (completion_tokens / 1_000_000) * BASELINE_RATE_PER_1M["output"]
        )
        savings = max(0.0, baseline_cost - actual_cost_usd)
        pipe.incrbyfloat(KEYS["estimated_savings_usd"], savings)

        if fallback_used:
            pipe.incr(KEYS["fallbacks_triggered"])

        await pipe.execute()

    async def record_circuit_trip(self):
        await self.redis.incr(KEYS["circuit_trips"])

    async def snapshot(self) -> dict:
        pipe = self.redis.pipeline()
        for k in KEYS.values():
            pipe.get(k)
        values = await pipe.execute()

        result = {}
        for key_name, raw in zip(KEYS.keys(), values):
            if raw is None:
                result[key_name] = 0
            else:
                result[key_name] = float(raw) if "." in raw or "usd" in key_name else int(float(raw))

        total = result["total_requests"] or 1
        cache_hits = result["cache_hits_exact"] + result["cache_hits_semantic"]
        result["cache_hit_rate"] = round(cache_hits / total, 4)
        result["avg_cost_per_request_usd"] = round(result["total_cost_usd"] / total, 6)
        return result
