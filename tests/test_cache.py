import pytest
import pytest_asyncio
from redis.asyncio import Redis

from app.core.cache import ExactCache, SemanticCache


@pytest_asyncio.fixture
async def redis_client():
    r = Redis.from_url("redis://localhost:6379/15", decode_responses=True)  # db 15 = test db
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


@pytest.mark.asyncio
async def test_exact_cache_hit(redis_client):
    cache = ExactCache(redis_client, ttl_seconds=60)
    await cache.set("hello world", "simple", "response text", 2, 3, "mock-small", "mock")

    hit = await cache.get("hello world", "simple")
    assert hit is not None
    assert hit.response == "response text"
    assert hit.cache_type == "exact"


@pytest.mark.asyncio
async def test_exact_cache_is_normalized(redis_client):
    cache = ExactCache(redis_client, ttl_seconds=60)
    await cache.set("  Hello   World  ", "simple", "resp", 1, 1, "m", "p")
    hit = await cache.get("hello world", "simple")
    assert hit is not None


@pytest.mark.asyncio
async def test_exact_cache_miss_different_tier(redis_client):
    cache = ExactCache(redis_client, ttl_seconds=60)
    await cache.set("hello world", "simple", "resp", 1, 1, "m", "p")
    hit = await cache.get("hello world", "complex")
    assert hit is None


@pytest.mark.asyncio
async def test_semantic_cache_paraphrase_hit(redis_client):
    cache = SemanticCache(redis_client, threshold=0.8, max_entries=100)
    await cache.set(
        "What is the capital of France and why is it historically significant",
        "simple", "Paris is the capital", 5, 5, "m", "p",
    )
    hit = await cache.get(
        "What is the capital of France, and why is it historically significant?", "simple",
    )
    assert hit is not None
    assert hit.cache_type == "semantic"
    assert hit.similarity >= 0.8


@pytest.mark.asyncio
async def test_semantic_cache_unrelated_miss(redis_client):
    cache = SemanticCache(redis_client, threshold=0.9, max_entries=100)
    await cache.set("What is the capital of France", "simple", "Paris", 3, 1, "m", "p")
    hit = await cache.get("Write me a poem about the ocean waves", "simple")
    assert hit is None
