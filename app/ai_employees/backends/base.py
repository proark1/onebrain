"""Normalized model-backend contract owned by OneBrain."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Protocol


BACKEND_EVENT_TYPES = frozenset({
    "text",
    "structured_output",
    "tool_request",
    "usage",
    "warning",
    "error",
    "done",
})


class BackendUnavailableError(RuntimeError):
    """No configured backend may process the requested turn."""


@dataclass(frozen=True)
class AgentBackendRequest:
    model: str
    messages: tuple[dict[str, str], ...]
    max_output_tokens: int
    tools: tuple[dict, ...] = ()
    response_schema: dict = field(default_factory=dict)
    temperature: float = 0.2
    timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("Agent backend model is required.")
        if not self.messages:
            raise ValueError("Agent backend messages cannot be empty.")
        if not 1 <= self.max_output_tokens <= 32_768:
            raise ValueError("Agent backend max_output_tokens must be between 1 and 32768.")
        if not 0.01 <= self.timeout_seconds <= 900:
            raise ValueError("Agent backend timeout_seconds must be between 0.01 and 900.")
        if not 0 <= self.temperature <= 2:
            raise ValueError("Agent backend temperature must be between 0 and 2.")


@dataclass(frozen=True)
class BackendEvent:
    type: str
    text: str = ""
    data: dict = field(default_factory=dict)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float | None = None
    provider_session_ref: str = ""
    code: str = ""

    def __post_init__(self) -> None:
        if self.type not in BACKEND_EVENT_TYPES:
            raise ValueError(f"Unknown AI employee backend event: {self.type}")


class AgentBackend(Protocol):
    provider: str
    available: bool
    unavailable_reason: str

    def stream(self, request: AgentBackendRequest) -> Iterator[BackendEvent]: ...
