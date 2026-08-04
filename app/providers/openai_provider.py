from typing import List

import httpx

from app.models.schemas import ChatMessage
from app.providers.base import BaseProvider, ProviderError, ProviderResponse

PRICING = {
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o": {"input": 2.50, "output": 10.00},
}

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"


class OpenAIProvider(BaseProvider):
    name = "openai"

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
                resp = await client.post(OPENAI_API_URL, json=payload, headers=headers)
        except httpx.HTTPError as e:
            raise ProviderError(f"openai transport error: {e}") from e

        if resp.status_code >= 400:
            raise ProviderError(f"openai returned {resp.status_code}: {resp.text[:300]}")

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
        rates = PRICING.get(model, {"input": 1.0, "output": 2.0})
        return (prompt_tokens / 1_000_000) * rates["input"] + (completion_tokens / 1_000_000) * rates["output"]
