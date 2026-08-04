"""
The router is the heart of NeuroRouter. For every request it:

  1. Resolves which "tier" to use (auto-classify, or an explicit tier/model
     the caller forced).
  2. Checks the exact cache, then the semantic cache.
  3. On a miss, walks an ordered fallback chain of (provider, model) pairs
     for that tier. Each hop is gated by that provider's circuit breaker.
     If a provider is open (tripped) or the call itself fails, we move to
     the next hop instead of failing the whole request.
  4. Writes the result back into both cache tiers and records cost/usage
     stats.

The fallback chain is intentionally provider-agnostic: MockProvider slots
into the exact same chain as GroqProvider/OpenAIProvider, so the system is
fully runnable/demoable with zero API keys and upgrades to real providers
with no code changes - only the chain configuration changes based on which
API keys are present at startup.
"""
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from app.core.cache import ExactCache, SemanticCache
from app.core.circuit_breaker import CircuitBreaker, CircuitOpenError
from app.core.classifier import classify
from app.core.cost_tracker import CostTracker
from app.core.event_log import EventLogger
from app.models.schemas import ChatMessage, Complexity, RoutingMeta
from app.providers.base import BaseProvider, ProviderError, ProviderResponse


@dataclass
class ChainHop:
    provider_name: str
    model: str


@dataclass
class RouteResult:
    content: str
    prompt_tokens: int
    completion_tokens: int
    model: str
    provider: str
    routing: RoutingMeta


class NoAvailableProviderError(Exception):
    pass


class ModelRouter:
    def __init__(
        self,
        providers: Dict[str, BaseProvider],
        circuit_breakers: Dict[str, CircuitBreaker],
        tier_chains: Dict[Complexity, List[ChainHop]],
        exact_cache: ExactCache,
        semantic_cache: SemanticCache,
        cost_tracker: CostTracker,
        event_logger: Optional[EventLogger] = None,
    ):
        self.providers = providers
        self.circuit_breakers = circuit_breakers
        self.tier_chains = tier_chains
        self.exact_cache = exact_cache
        self.semantic_cache = semantic_cache
        self.cost_tracker = cost_tracker
        self.event_logger = event_logger

    def _resolve_tier(self, requested_model: str, prompt: str) -> Tuple[Complexity, float]:
        if requested_model == "auto":
            result = classify(prompt)
            return result.complexity, result.score

        if requested_model in (Complexity.simple, Complexity.medium, Complexity.complex):
            return Complexity(requested_model), -1.0

        # explicit model name forced by the caller - still route it through
        # the tier whose chain contains that exact model, so caching/cost
        # tracking behave consistently. Default to 'complex' bucket if
        # not found (fail safe toward the more capable chain).
        for tier, chain in self.tier_chains.items():
            if any(hop.model == requested_model for hop in chain):
                return tier, -1.0
        return Complexity.complex, -1.0

    async def route(self, messages: List[ChatMessage], requested_model: str, temperature: float,
                     max_tokens: int, use_cache: bool, trace_id: Optional[str] = None,
                     disabled_providers: Optional[set] = None, api_key_name: Optional[str] = None) -> RouteResult:
        disabled_providers = disabled_providers or set()
        start = time.perf_counter()
        user_prompt = next((m.content for m in reversed(messages) if m.role == "user"), "")

        tier, complexity_score = self._resolve_tier(requested_model, user_prompt)
        if complexity_score < 0:
            # forced tier - still compute a score for observability
            complexity_score = classify(user_prompt).score

        # ---- Cache lookup ----
        if use_cache:
            hit = await self.exact_cache.get(user_prompt, tier.value)
            if hit is None:
                hit = await self.semantic_cache.get(user_prompt, tier.value)

            if hit is not None:
                latency_ms = (time.perf_counter() - start) * 1000
                await self.cost_tracker.record_request(
                    cache_hit=True,
                    cache_type=hit.cache_type,
                    prompt_tokens=hit.prompt_tokens,
                    completion_tokens=hit.completion_tokens,
                    actual_cost_usd=0.0,  # cache hits cost nothing
                )
                if self.event_logger:
                    await self.event_logger.log(
                        level="info", type_="request",
                        message=f"Cache hit ({hit.cache_type}) served from {hit.provider}/{hit.model}",
                        trace_id=trace_id, provider=hit.provider, model=hit.model, tier=tier.value,
                        cache_hit=True, cache_type=hit.cache_type, latency_ms=round(latency_ms, 2),
                        cost_usd=0.0, status="200 OK", api_key_name=api_key_name,
                        prompt_preview=user_prompt, response_preview=hit.response,
                    )
                return RouteResult(
                    content=hit.response,
                    prompt_tokens=hit.prompt_tokens,
                    completion_tokens=hit.completion_tokens,
                    model=hit.model,
                    provider=hit.provider,
                    routing=RoutingMeta(
                        complexity=tier,
                        complexity_score=complexity_score,
                        model_used=hit.model,
                        provider_used=hit.provider,
                        cache_hit=True,
                        cache_type=hit.cache_type,
                        latency_ms=round(latency_ms, 2),
                        estimated_cost_usd=0.0,
                    ),
                )

        # ---- Fallback chain over live providers ----
        chain = self.tier_chains.get(tier, [])
        if not chain:
            raise NoAvailableProviderError(f"no chain configured for tier {tier}")

        last_error: Optional[Exception] = None
        attempts = 0

        for hop in chain:
            if hop.provider_name in disabled_providers:
                if self.event_logger:
                    await self.event_logger.log(
                        level="warn", type_="provider_error",
                        message=f"Skipped {hop.provider_name}/{hop.model} — disabled by admin",
                        trace_id=trace_id, provider=hop.provider_name, model=hop.model, tier=tier.value,
                        status="skipped", api_key_name=api_key_name,
                    )
                continue

            attempts += 1
            provider = self.providers.get(hop.provider_name)
            breaker = self.circuit_breakers.get(hop.provider_name)
            if provider is None or breaker is None:
                continue

            was_open_before = (await breaker.get_state()).value != "closed"

            try:
                await breaker.before_call()
                response: ProviderResponse = await provider.complete(
                    messages=messages, model=hop.model, temperature=temperature, max_tokens=max_tokens,
                )
                await breaker.record_success()
            except (ProviderError, CircuitOpenError) as e:
                last_error = e
                is_trip = isinstance(e, ProviderError)
                if breaker is not None and is_trip:
                    try:
                        await breaker.record_failure()
                    except Exception:
                        pass
                if self.event_logger:
                    new_state = (await breaker.get_state()).value if breaker else "unknown"
                    tripped_now = is_trip and new_state == "open" and not was_open_before
                    await self.event_logger.log(
                        level="error" if tripped_now else "warn",
                        type_="circuit_trip" if tripped_now else "provider_error",
                        message=(f"Circuit tripped OPEN for {hop.provider_name} after repeated failures"
                                 if tripped_now else f"{hop.provider_name}/{hop.model} failed: {e}"),
                        trace_id=trace_id, provider=hop.provider_name, model=hop.model, tier=tier.value,
                        status="error", api_key_name=api_key_name,
                    )
                continue

            # success
            latency_ms = (time.perf_counter() - start) * 1000
            cost = provider.estimate_cost(hop.model, response.prompt_tokens, response.completion_tokens)
            fallback_used = attempts > 1

            if use_cache:
                await self.exact_cache.set(
                    user_prompt, tier.value, response.content, response.prompt_tokens,
                    response.completion_tokens, response.model, response.provider,
                )
                await self.semantic_cache.set(
                    user_prompt, tier.value, response.content, response.prompt_tokens,
                    response.completion_tokens, response.model, response.provider,
                )

            await self.cost_tracker.record_request(
                cache_hit=False,
                cache_type=None,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                actual_cost_usd=cost,
                fallback_used=fallback_used,
            )

            if self.event_logger:
                await self.event_logger.log(
                    level="warn" if fallback_used else "info", type_="request",
                    message=(f"Routed to {response.provider}/{response.model}"
                             + (" (fallback hop)" if fallback_used else "")),
                    trace_id=trace_id, provider=response.provider, model=response.model, tier=tier.value,
                    cache_hit=False, latency_ms=round(latency_ms, 2), cost_usd=round(cost, 8),
                    status="200 OK", api_key_name=api_key_name,
                    prompt_preview=user_prompt, response_preview=response.content,
                )

            return RouteResult(
                content=response.content,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                model=response.model,
                provider=response.provider,
                routing=RoutingMeta(
                    complexity=tier,
                    complexity_score=complexity_score,
                    model_used=response.model,
                    provider_used=response.provider,
                    cache_hit=False,
                    latency_ms=round(latency_ms, 2),
                    estimated_cost_usd=round(cost, 8),
                    fallback_used=fallback_used,
                    attempts=attempts,
                ),
            )

        if self.event_logger:
            await self.event_logger.log(
                level="error", type_="provider_error",
                message=f"All providers exhausted for tier '{tier.value}': {last_error}",
                trace_id=trace_id, tier=tier.value, status="503", api_key_name=api_key_name,
                prompt_preview=user_prompt,
            )

        raise NoAvailableProviderError(
            f"all providers in chain for tier '{tier.value}' failed/unavailable: {last_error}"
        )
