from enum import Enum
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class Complexity(str, Enum):
    simple = "simple"
    medium = "medium"
    complex = "complex"


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatCompletionRequest(BaseModel):
    model: str = Field(
        default="auto",
        description="'auto' lets the router pick a model tier. "
        "Or force one of: simple | medium | complex | <provider-specific model name>",
    )
    messages: List[ChatMessage]
    temperature: float = 0.7
    max_tokens: int = 1024
    stream: bool = False
    # allow client to opt out of cache for a given request (e.g. testing)
    use_cache: bool = True


class UsageInfo(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class RoutingMeta(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    complexity: Complexity
    complexity_score: float
    model_used: str
    provider_used: str
    cache_hit: bool
    cache_type: Optional[str] = None
    latency_ms: float
    estimated_cost_usd: float
    fallback_used: bool = False
    attempts: int = 1


class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: str = "stop"


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[ChatCompletionChoice]
    usage: UsageInfo
    routing: RoutingMeta
