from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from redis.asyncio import Redis

from app.api.admin_routes import router as admin_router
from app.api.logs_routes import router as logs_router
from app.api.routes import router as api_router
from app.config import get_settings
from app.core.api_keys import ApiKeyStore
from app.core.cost_tracker import CostTracker
from app.core.event_log import EventLogger
from app.core.provider_control import ProviderControl
from app.core.router import ChainHop
from app.core.runtime_config import RuntimeConfig
from app.models.schemas import Complexity
from app.providers.groq_provider import GroqProvider
from app.providers.mock_provider import MockProvider
from app.providers.openai_provider import OpenAIProvider
from app.utils.logger import get_logger

logger = get_logger("neurorouter.main")


def build_providers(settings):
    """
    Wire up whichever providers have API keys configured. If none do, fall
    back to MockProvider so the gateway is fully runnable out of the box.
    """
    providers = {}

    if settings.groq_api_key:
        providers["groq"] = GroqProvider(settings.groq_api_key)
        logger.info("Groq provider enabled")

    if settings.openai_api_key:
        providers["openai"] = OpenAIProvider(settings.openai_api_key)
        logger.info("OpenAI provider enabled (fallback)")

    if not providers:
        providers["mock"] = MockProvider()
        logger.warning("No provider API keys found - running with MockProvider only. "
                        "Set GROQ_API_KEY in .env for real LLM calls.")

    return providers


def build_tier_chains(providers: dict):
    """
    Maps each complexity tier to an ORDERED fallback chain of (provider,
    model) hops. The router tries each hop in order, skipping any whose
    circuit breaker is open or that an admin has disabled.
    """
    if "groq" in providers:
        small, large = "llama-3.1-8b-instant", "llama-3.3-70b-versatile"
        chains = {
            Complexity.simple: [ChainHop("groq", small)],
            Complexity.medium: [ChainHop("groq", small)],
            Complexity.complex: [ChainHop("groq", large)],
        }
        if "openai" in providers:
            chains[Complexity.simple].append(ChainHop("openai", "gpt-4o-mini"))
            chains[Complexity.medium].append(ChainHop("openai", "gpt-4o-mini"))
            chains[Complexity.complex].append(ChainHop("openai", "gpt-4o"))
        return chains

    if "openai" in providers:
        return {
            Complexity.simple: [ChainHop("openai", "gpt-4o-mini")],
            Complexity.medium: [ChainHop("openai", "gpt-4o-mini")],
            Complexity.complex: [ChainHop("openai", "gpt-4o")],
        }

    # mock-only mode
    return {
        Complexity.simple: [ChainHop("mock", "mock-small")],
        Complexity.medium: [ChainHop("mock", "mock-small")],
        Complexity.complex: [ChainHop("mock", "mock-large")],
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)

    providers = build_providers(settings)
    tier_chains = build_tier_chains(providers)

    cost_tracker = CostTracker(redis)
    event_logger = EventLogger(redis)
    api_key_store = ApiKeyStore(redis)
    runtime_config = RuntimeConfig(redis, settings)
    provider_control = ProviderControl(redis)

    await api_key_store.seed_if_empty(settings.api_keys)

    # circuit_breakers dict kept here for /v1/admin/providers/*/reset-circuit;
    # the request-path builds its own fresh instances so config changes apply live.
    from app.core.circuit_breaker import CircuitBreaker
    cfg = await runtime_config.get()
    circuit_breakers = {
        name: CircuitBreaker(redis, name, failure_threshold=cfg["circuit_failure_threshold"],
                              recovery_seconds=cfg["circuit_recovery_seconds"])
        for name in providers
    }

    app.state.settings = settings
    app.state.redis = redis
    app.state.providers = providers
    app.state.tier_chains = tier_chains
    app.state.circuit_breakers = circuit_breakers
    app.state.cost_tracker = cost_tracker
    app.state.event_logger = event_logger
    app.state.api_key_store = api_key_store
    app.state.runtime_config = runtime_config
    app.state.provider_control = provider_control

    logger.info(f"NeuroRouter started. Providers: {list(providers.keys())}")
    yield

    await redis.aclose()
    logger.info("NeuroRouter shut down.")


def create_app() -> FastAPI:
    app = FastAPI(
        title="NeuroRouter",
        description="LLM Gateway / Model Router - complexity-based routing, "
                     "semantic caching, rate limiting, and circuit breakers.",
        version="1.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router)
    app.include_router(admin_router)
    app.include_router(logs_router)

    # Frontend (control-panel UI) - mounted LAST so it acts as a catch-all
    # for everything not matched by an API route above.
    try:
        app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
    except RuntimeError:
        pass  # frontend dir not present (e.g. during certain test runs)

    return app


app = create_app()
