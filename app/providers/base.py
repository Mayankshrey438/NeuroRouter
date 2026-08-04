from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List

from app.models.schemas import ChatMessage


@dataclass
class ProviderResponse:
    content: str
    prompt_tokens: int
    completion_tokens: int
    model: str
    provider: str


class ProviderError(Exception):
    """Raised when a provider call fails (timeout, 5xx, auth error, etc)."""


class BaseProvider(ABC):
    name: str = "base"

    @abstractmethod
    async def complete(
        self,
        messages: List[ChatMessage],
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> ProviderResponse:
        ...

    @abstractmethod
    def estimate_cost(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        ...
