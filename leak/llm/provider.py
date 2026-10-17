"""Provider-neutral LLM types.

The harness speaks only in these types. Each provider (Gemini, Ollama, the
scripted fake used in tests) translates them to and from its own SDK. That
keeps `google.genai` imports out of `leak/harness/` entirely, and it means a
trace written with one provider can be replayed without that provider's SDK.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    """The model asking us to run a tool."""

    id: str
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    # Opaque provider data that must be echoed back on the next turn
    # (e.g. Gemini 3 "thought signatures"). The harness never reads it.
    provider_meta: dict[str, str] = Field(default_factory=dict)


class ToolResult(BaseModel):
    """What we tell the model after (not) running a tool."""

    call_id: str
    name: str
    content: Any
    is_error: bool = False


class Message(BaseModel):
    role: Literal["user", "assistant", "tool"]
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_results: list[ToolResult] = Field(default_factory=list)


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0  # includes "thinking" tokens; they are billed as output
    cached_tokens: int = 0  # subset of input_tokens served from a context cache


class LLMResponse(BaseModel):
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    model: str
    finish_reason: str | None = None


class ToolSpec(BaseModel):
    """A tool as the model sees it: name, description, JSON schema of args."""

    name: str
    description: str
    parameters: dict[str, Any]


class LLMError(RuntimeError):
    """Raised by providers after retries are exhausted."""


class LLMProvider(Protocol):
    name: str

    def generate(
        self,
        *,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        response_schema: type[BaseModel] | None = None,
    ) -> LLMResponse: ...
