import asyncio

import pytest
import pytest_asyncio
from redis.asyncio import Redis

from app.core.circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState
from app.core.rate_limiter import RateLimitExceeded, RateLimiter


@pytest_asyncio.fixture
async def redis_client():
    r = Redis.from_url("redis://localhost:6379/15", decode_responses=True)
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


@pytest.mark.asyncio
async def test_rate_limiter_allows_up_to_limit(redis_client):
    limiter = RateLimiter(redis_client, limit=3, window_seconds=10)
    for _ in range(3):
        await limiter.check("key1")  # should not raise


@pytest.mark.asyncio
async def test_rate_limiter_blocks_over_limit(redis_client):
    limiter = RateLimiter(redis_client, limit=2, window_seconds=10)
    await limiter.check("key2")
    await limiter.check("key2")
    with pytest.raises(RateLimitExceeded):
        await limiter.check("key2")


@pytest.mark.asyncio
async def test_rate_limiter_keys_independent(redis_client):
    limiter = RateLimiter(redis_client, limit=1, window_seconds=10)
    await limiter.check("a")
    await limiter.check("b")  # different key, should not raise


@pytest.mark.asyncio
async def test_circuit_breaker_opens_after_threshold(redis_client):
    cb = CircuitBreaker(redis_client, "p1", failure_threshold=2, recovery_seconds=60)
    assert await cb.get_state() == CircuitState.CLOSED
    await cb.record_failure()
    assert await cb.get_state() == CircuitState.CLOSED
    await cb.record_failure()
    assert await cb.get_state() == CircuitState.OPEN

    with pytest.raises(CircuitOpenError):
        await cb.before_call()


@pytest.mark.asyncio
async def test_circuit_breaker_half_opens_after_recovery(redis_client):
    cb = CircuitBreaker(redis_client, "p2", failure_threshold=1, recovery_seconds=1)
    await cb.record_failure()
    assert await cb.get_state() == CircuitState.OPEN
    await asyncio.sleep(1.1)
    assert await cb.get_state() == CircuitState.HALF_OPEN


@pytest.mark.asyncio
async def test_circuit_breaker_recovers_on_success(redis_client):
    cb = CircuitBreaker(redis_client, "p3", failure_threshold=1, recovery_seconds=1)
    await cb.record_failure()
    await asyncio.sleep(1.1)
    await cb.before_call()  # half-open probe
    await cb.record_success()
    assert await cb.get_state() == CircuitState.CLOSED
