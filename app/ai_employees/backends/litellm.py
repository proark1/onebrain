"""Gemini/Anthropic chat adapter through LiteLLM's normalized API."""

from __future__ import annotations

from app.ai_employees.backends.base import AgentBackendRequest, BackendEvent
from app.llm.pricing import estimate_cost


class LiteLLMAgentBackend:
    def __init__(
        self,
        provider: str,
        *,
        available: bool,
        unavailable_reason: str = "",
        client=None,
    ):
        self.provider = provider
        self.available = available
        self.unavailable_reason = unavailable_reason
        if client is None:
            import litellm

            client = litellm
        self._client = client

    def stream(self, request: AgentBackendRequest):
        kwargs = {
            "model": request.model,
            "messages": list(request.messages),
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
            "timeout": request.timeout_seconds,
        }
        if request.tools:
            kwargs["tools"] = list(request.tools)
        if request.response_schema:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": request.response_schema,
            }
        usage = None
        session_ref = ""
        for part in self._client.completion(**kwargs):
            session_ref = str(getattr(part, "id", "") or session_ref)
            found_usage = getattr(part, "usage", None)
            if found_usage is not None:
                usage = found_usage
            choices = getattr(part, "choices", None) or []
            if not choices:
                continue
            delta = getattr(choices[0], "delta", None)
            content = getattr(delta, "content", None)
            if content:
                yield BackendEvent(type="text", text=str(content))
            tool_calls = getattr(delta, "tool_calls", None) or []
            for tool_call in tool_calls:
                function = getattr(tool_call, "function", None)
                yield BackendEvent(type="tool_request", data={
                    "id": str(getattr(tool_call, "id", "") or ""),
                    "name": str(getattr(function, "name", "") or ""),
                    "arguments": str(getattr(function, "arguments", "") or ""),
                })
        if usage is not None:
            prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
            yield BackendEvent(
                type="usage",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_usd=estimate_cost(request.model, prompt_tokens, completion_tokens),
                provider_session_ref=session_ref,
            )
        yield BackendEvent(type="done")
