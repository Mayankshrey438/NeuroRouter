"""
Lightweight structured event log, backed by a capped Redis list.

Every chat completion (cache hit or live call), provider failure, circuit
trip, and rate-limit rejection gets pushed here as a small JSON record.
This is what powers the Live Monitor table, the System Logs stream, and
the Request Trace Details view in the frontend - all three are just
different renderings/filters over the same underlying event stream.

Kept intentionally small: capped list (default 1000), truncated
prompt/response previews (not full payloads) to stay light in Redis and
avoid persisting large amounts of user content in a demo tool.
"""
import json
import time
import uuid
from typing import Optional

from redis.asyncio import Redis

LIST_KEY = "logs:events"
MAX_ENTRIES = 1000


def _preview(text: str, n: int = 180) -> str:
    text = text or ""
    return text if len(text) <= n else text[:n] + "…"


class EventLogger:
    def __init__(self, redis: Redis):
        self.redis = redis

    async def log(
        self,
        level: str,  # "info" | "warn" | "error"
        type_: str,  # "request" | "provider_error" | "circuit_trip" | "rate_limit" | "auth_fail"
        message: str,
        trace_id: Optional[str] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        tier: Optional[str] = None,
        cache_hit: Optional[bool] = None,
        cache_type: Optional[str] = None,
        latency_ms: Optional[float] = None,
        cost_usd: Optional[float] = None,
        status: Optional[str] = None,
        api_key_name: Optional[str] = None,
        prompt_preview: Optional[str] = None,
        response_preview: Optional[str] = None,
    ):
        entry = {
            "id": trace_id or f"evt_{uuid.uuid4().hex[:12]}",
            "ts": time.time(),
            "level": level,
            "type": type_,
            "message": message,
            "provider": provider,
            "model": model,
            "tier": tier,
            "cache_hit": cache_hit,
            "cache_type": cache_type,
            "latency_ms": latency_ms,
            "cost_usd": cost_usd,
            "status": status,
            "api_key_name": api_key_name,
            "prompt_preview": _preview(prompt_preview) if prompt_preview else None,
            "response_preview": _preview(response_preview) if response_preview else None,
        }
        pipe = self.redis.pipeline()
        pipe.lpush(LIST_KEY, json.dumps(entry))
        pipe.ltrim(LIST_KEY, 0, MAX_ENTRIES - 1)
        await pipe.execute()
        return entry

    async def recent(self, limit: int = 50, level: Optional[str] = None, type_: Optional[str] = None) -> list:
        raw = await self.redis.lrange(LIST_KEY, 0, MAX_ENTRIES - 1)
        entries = [json.loads(r) for r in raw]
        if level:
            entries = [e for e in entries if e["level"] == level]
        if type_:
            entries = [e for e in entries if e["type"] == type_]
        return entries[:limit]

    async def get(self, trace_id: str) -> Optional[dict]:
        raw = await self.redis.lrange(LIST_KEY, 0, MAX_ENTRIES - 1)
        for r in raw:
            entry = json.loads(r)
            if entry["id"] == trace_id:
                return entry
        return None
