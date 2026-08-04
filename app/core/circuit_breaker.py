"""
Redis-backed circuit breaker.

Classic 3-state machine per provider:

  CLOSED  -> requests flow normally. Failures are counted in a rolling window.
  OPEN    -> too many failures; short-circuit immediately (no network call),
             fail fast so the router can fall back to the next provider.
  HALF_OPEN -> after the recovery window elapses, allow a single trial
             request through. Success -> CLOSED. Failure -> OPEN again.

State lives in Redis (not in-process memory) so it's correct across multiple
uvicorn workers / gateway replicas - important since NeuroRouter is meant to
be horizontally scaled behind a load balancer.
"""
import time
from enum import Enum

from redis.asyncio import Redis


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    pass


class CircuitBreaker:
    def __init__(
        self,
        redis: Redis,
        provider_name: str,
        failure_threshold: int = 5,
        recovery_seconds: int = 30,
        window_seconds: int = 60,
    ):
        self.redis = redis
        self.provider_name = provider_name
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self.window_seconds = window_seconds

        self._failures_key = f"cb:{provider_name}:failures"
        self._opened_at_key = f"cb:{provider_name}:opened_at"
        self._half_open_probe_key = f"cb:{provider_name}:probe_inflight"

    async def get_state(self) -> CircuitState:
        opened_at = await self.redis.get(self._opened_at_key)
        if opened_at is None:
            return CircuitState.CLOSED

        elapsed = time.time() - float(opened_at)
        if elapsed >= self.recovery_seconds:
            return CircuitState.HALF_OPEN
        return CircuitState.OPEN

    async def before_call(self):
        """Raise CircuitOpenError if the call should be short-circuited."""
        state = await self.get_state()

        if state == CircuitState.OPEN:
            raise CircuitOpenError(f"circuit open for provider '{self.provider_name}'")

        if state == CircuitState.HALF_OPEN:
            # Only let ONE probe request through at a time
            acquired = await self.redis.set(self._half_open_probe_key, "1", nx=True, ex=self.recovery_seconds)
            if not acquired:
                raise CircuitOpenError(f"circuit half-open, probe in flight for '{self.provider_name}'")

    async def record_success(self):
        pipe = self.redis.pipeline()
        pipe.delete(self._failures_key)
        pipe.delete(self._opened_at_key)
        pipe.delete(self._half_open_probe_key)
        await pipe.execute()

    async def record_failure(self):
        pipe = self.redis.pipeline()
        pipe.incr(self._failures_key)
        pipe.expire(self._failures_key, self.window_seconds)
        await pipe.execute()
        failures = int(await self.redis.get(self._failures_key) or 0)

        if failures >= self.failure_threshold:
            await self.redis.set(self._opened_at_key, time.time())

        await self.redis.delete(self._half_open_probe_key)

    async def snapshot(self) -> dict:
        state = await self.get_state()
        failures = int(await self.redis.get(self._failures_key) or 0)
        return {"provider": self.provider_name, "state": state.value, "recent_failures": failures}
