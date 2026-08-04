"""
Two-tier caching:

1. Exact cache  - sha256(normalized_prompt + model) -> response, stored as a
   plain Redis string with a TTL. O(1) lookup, catches identical repeat
   prompts (extremely common in production - retries, polling, shared demos).

2. Semantic cache - catches *paraphrased* duplicates ("what's the capital of
   France?" vs "tell me France's capital city") that an exact hash would
   miss. We avoid pulling in a transformer embedding model (heavy download,
   GPU-friendly but not required here) and instead use scikit-learn's
   HashingVectorizer: a stateless, fixed-dimensional bag-of-words hash that
   needs no fitting/training and no network access, then compare via cosine
   similarity against a bounded recent-prompt window kept in Redis. This is
   a deliberate lightweight-first-then-upgrade choice: the interface below
   (`SemanticCache`) is the seam where you'd later swap in a real sentence
   embedding model without touching the router.
"""
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np
from redis.asyncio import Redis
from sklearn.feature_extraction.text import HashingVectorizer

_vectorizer = HashingVectorizer(n_features=256, alternate_sign=False, norm="l2")


def _normalize(prompt: str) -> str:
    return " ".join(prompt.strip().lower().split())


def _exact_key(prompt: str, model_tier: str) -> str:
    digest = hashlib.sha256(f"{model_tier}:{_normalize(prompt)}".encode()).hexdigest()
    return f"cache:exact:{digest}"


@dataclass
class CacheHit:
    response: str
    prompt_tokens: int
    completion_tokens: int
    model: str
    provider: str
    cache_type: str  # "exact" | "semantic"
    similarity: Optional[float] = None


class ExactCache:
    def __init__(self, redis: Redis, ttl_seconds: int):
        self.redis = redis
        self.ttl_seconds = ttl_seconds

    async def get(self, prompt: str, model_tier: str) -> Optional[CacheHit]:
        raw = await self.redis.get(_exact_key(prompt, model_tier))
        if raw is None:
            return None
        data = json.loads(raw)
        return CacheHit(**data, cache_type="exact")

    async def set(self, prompt: str, model_tier: str, response: str, prompt_tokens: int,
                   completion_tokens: int, model: str, provider: str):
        payload = json.dumps({
            "response": response,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "model": model,
            "provider": provider,
        })
        await self.redis.set(_exact_key(prompt, model_tier), payload, ex=self.ttl_seconds)


class SemanticCache:
    def __init__(self, redis: Redis, threshold: float, max_entries: int):
        self.redis = redis
        self.threshold = threshold
        self.max_entries = max_entries
        self._list_key = "cache:semantic:entries"

    def _vectorize(self, prompt: str) -> np.ndarray:
        return _vectorizer.transform([_normalize(prompt)]).toarray()[0]

    async def get(self, prompt: str, model_tier: str) -> Optional[CacheHit]:
        raw_entries = await self.redis.lrange(self._list_key, 0, self.max_entries - 1)
        if not raw_entries:
            return None

        query_vec = self._vectorize(prompt)
        best_sim = -1.0
        best_entry = None

        for raw in raw_entries:
            entry = json.loads(raw)
            if entry.get("model_tier") != model_tier:
                continue
            candidate_vec = np.array(entry["vector"], dtype=np.float32)
            denom = (np.linalg.norm(query_vec) * np.linalg.norm(candidate_vec)) or 1e-9
            sim = float(np.dot(query_vec, candidate_vec) / denom)
            if sim > best_sim:
                best_sim = sim
                best_entry = entry

        if best_entry is not None and best_sim >= self.threshold:
            return CacheHit(
                response=best_entry["response"],
                prompt_tokens=best_entry["prompt_tokens"],
                completion_tokens=best_entry["completion_tokens"],
                model=best_entry["model"],
                provider=best_entry["provider"],
                cache_type="semantic",
                similarity=round(best_sim, 4),
            )
        return None

    async def set(self, prompt: str, model_tier: str, response: str, prompt_tokens: int,
                   completion_tokens: int, model: str, provider: str):
        vec = self._vectorize(prompt).tolist()
        entry = json.dumps({
            "prompt": prompt,
            "model_tier": model_tier,
            "vector": vec,
            "response": response,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "model": model,
            "provider": provider,
            "ts": time.time(),
        })
        pipe = self.redis.pipeline()
        pipe.lpush(self._list_key, entry)
        pipe.ltrim(self._list_key, 0, self.max_entries - 1)
        await pipe.execute()
