import asyncio
import hashlib
import random
from typing import List

from app.models.schemas import ChatMessage
from app.providers.base import BaseProvider, ProviderResponse

# Fake pricing so cost-savings demos still produce meaningful numbers
PRICING = {
    "mock-small": {"input": 0.05, "output": 0.08},
    "mock-large": {"input": 0.60, "output": 0.80},
}


class MockProvider(BaseProvider):
    """
    Deterministic offline stand-in for a real LLM provider.

    Lets you run the entire gateway - routing, caching, rate limiting,
    circuit breaking, cost tracking - with zero API keys configured.
    Swap in GroqProvider/OpenAIProvider once you have a key and nothing
    else in the router changes.
    """

    name = "mock"

    async def complete(
        self,
        messages: List[ChatMessage],
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> ProviderResponse:
        # simulate network latency so circuit breaker / latency metrics are meaningful
        await asyncio.sleep(random.uniform(0.05, 0.2))

        last_user_msg = next((m.content for m in reversed(messages) if m.role == "user"), "")
        digest = hashlib.sha1(last_user_msg.encode()).hexdigest()[:8]
        reply = (
            f"[mock-{model} response #{digest}] This is a simulated completion "
            f"standing in for a real LLM call. Your prompt had "
            f"{len(last_user_msg.split())} words."
        )

        prompt_tokens = max(1, len(last_user_msg.split()))
        completion_tokens = max(1, len(reply.split()))

        return ProviderResponse(
            content=reply,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model=model,
            provider=self.name,
        )

    def estimate_cost(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        rates = PRICING.get(model, {"input": 0.1, "output": 0.2})
        return (prompt_tokens / 1_000_000) * rates["input"] + (completion_tokens / 1_000_000) * rates["output"]
