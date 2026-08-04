from typing import List

import httpx

from app.models.schemas import ChatMessage
from app.providers.base import BaseProvider, ProviderError, ProviderResponse

# Approximate public pricing (USD per 1M tokens) as of the model's release.
# These are ESTIMATES for cost-tracking/demo purposes, not billing-accurate.
PRICING = {
    "llama-3.1-8b-instant": {"input": 0.05, "output": 0.08},
    "llama-3.3-70b-versatile": {"input": 0.59, "output": 0.79},
}

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"


class GroqProvider(BaseProvider):
    name = "groq"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def complete(
        self,
        messages: List[ChatMessage],
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> ProviderResponse:
        payload = {
            "model": model,
            "messages": [m.model_dump() for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(GROQ_API_URL, json=payload, headers=headers)
        except httpx.HTTPError as e:
            raise ProviderError(f"groq transport error: {e}") from e

        if resp.status_code >= 400:
            raise ProviderError(f"groq returned {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        choice = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})

        return ProviderResponse(
            content=choice,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            model=model,
            provider=self.name,
        )

    def estimate_cost(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        rates = PRICING.get(model, {"input": 0.5, "output": 0.7})
        return (prompt_tokens / 1_000_000) * rates["input"] + (completion_tokens / 1_000_000) * rates["output"]
