import time
import uuid

from fastapi import APIRouter, Header, HTTPException, Request

from app.core.cache import ExactCache, SemanticCache
from app.core.circuit_breaker import CircuitBreaker
from app.core.rate_limiter import RateLimitExceeded, RateLimiter
from app.core.router import ModelRouter, NoAvailableProviderError
from app.models.schemas import (
    ChatCompletionChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    UsageInfo,
)
from app.utils.logger import get_logger

router = APIRouter()
logger = get_logger("neurorouter.api")


async def _authenticate(request: Request, x_api_key: str | None):
    """Returns the ApiKeyRecord for a valid key, or raises 401."""
    store = request.app.state.api_key_store
    record = await store.validate(x_api_key)
    if record is None:
        if request.app.state.event_logger:
            await request.app.state.event_logger.log(
                level="warn", type_="auth_fail",
                message="Rejected request with invalid or missing API key",
                status="401",
            )
        raise HTTPException(status_code=401, detail="invalid, missing, or revoked X-API-Key header")
    return record


def _build_model_router(app_state, cfg: dict) -> ModelRouter:
    """Builds a ModelRouter using the CURRENT runtime config values. Cheap:
    no I/O happens at construction time, only when methods are called."""
    exact_cache = ExactCache(app_state.redis, cfg["cache_ttl_seconds"])
    semantic_cache = SemanticCache(
        app_state.redis, cfg["semantic_cache_threshold"], cfg["semantic_cache_max_entries"]
    )
    circuit_breakers = {
        name: CircuitBreaker(
            app_state.redis, name,
            failure_threshold=cfg["circuit_failure_threshold"],
            recovery_seconds=cfg["circuit_recovery_seconds"],
        )
        for name in app_state.providers
    }
    return ModelRouter(
        providers=app_state.providers,
        circuit_breakers=circuit_breakers,
        tier_chains=app_state.tier_chains,
        exact_cache=exact_cache,
        semantic_cache=semantic_cache,
        cost_tracker=app_state.cost_tracker,
        event_logger=app_state.event_logger,
    )


@router.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(
    body: ChatCompletionRequest,
    request: Request,
    x_api_key: str | None = Header(default=None),
):
    key_record = await _authenticate(request, x_api_key)

    cfg = await request.app.state.runtime_config.get()

    rate_limiter = RateLimiter(request.app.state.redis, cfg["rate_limit_requests"], cfg["rate_limit_window_seconds"])
    identity = key_record.key
    try:
        await rate_limiter.check(identity)
    except RateLimitExceeded as e:
        if request.app.state.event_logger:
            await request.app.state.event_logger.log(
                level="warn", type_="rate_limit",
                message=f"Rate limit exceeded for key '{key_record.name}'",
                status="429", api_key_name=key_record.name,
            )
        raise HTTPException(
            status_code=429,
            detail=f"rate limit exceeded, retry after {e.retry_after_seconds:.1f}s",
            headers={"Retry-After": str(int(e.retry_after_seconds) + 1)},
        )

    await request.app.state.api_key_store.touch(key_record.key)

    trace_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    disabled_providers = await request.app.state.provider_control.disabled_set()
    model_router = _build_model_router(request.app.state, cfg)

    try:
        result = await model_router.route(
            messages=body.messages,
            requested_model=body.model,
            temperature=body.temperature,
            max_tokens=body.max_tokens,
            use_cache=body.use_cache,
            trace_id=trace_id,
            disabled_providers=disabled_providers,
            api_key_name=key_record.name,
        )
    except NoAvailableProviderError as e:
        logger.error(f"all providers exhausted: {e}")
        raise HTTPException(status_code=503, detail=f"no provider available: {e}")

    return ChatCompletionResponse(
        id=trace_id,
        created=int(time.time()),
        model=result.model,
        choices=[
            ChatCompletionChoice(message=ChatMessage(role="assistant", content=result.content))
        ],
        usage=UsageInfo(
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            total_tokens=result.prompt_tokens + result.completion_tokens,
        ),
        routing=result.routing,
    )


@router.get("/v1/stats")
async def stats(request: Request):
    cost_tracker = request.app.state.cost_tracker
    return await cost_tracker.snapshot()


@router.get("/v1/models")
async def list_models(request: Request):
    tier_chains = request.app.state.tier_chains
    cfg = await request.app.state.runtime_config.get()
    circuit_breakers = {
        name: CircuitBreaker(
            request.app.state.redis, name,
            failure_threshold=cfg["circuit_failure_threshold"],
            recovery_seconds=cfg["circuit_recovery_seconds"],
        )
        for name in request.app.state.providers
    }
    disabled = await request.app.state.provider_control.disabled_set()

    breaker_states = {}
    for name, breaker in circuit_breakers.items():
        snap = await breaker.snapshot()
        snap["disabled_by_admin"] = name in disabled
        breaker_states[name] = snap

    tiers = {
        tier.value: [{"provider": hop.provider_name, "model": hop.model} for hop in chain]
        for tier, chain in tier_chains.items()
    }
    return {"tiers": tiers, "circuit_breakers": breaker_states}


@router.get("/health")
async def health(request: Request):
    redis = request.app.state.redis
    try:
        await redis.ping()
        redis_ok = True
    except Exception:
        redis_ok = False

    return {
        "status": "ok" if redis_ok else "degraded",
        "redis_connected": redis_ok,
        "providers_configured": list(request.app.state.providers.keys()),
    }
