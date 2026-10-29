"""Provider-neutral LLM types.

The harness speaks only in these types. Each provider (OpenRouter, the
scripted fake used in tests, trace replay) translates them to and from its own
wire format. That keeps SDK imports out of `leak/harness/` entirely, and it
means a trace written with one provider can be replayed without that SDK.

`before_request` is the per-attempt gate: a provider that talks to the network
must call it before *every* HTTP attempt (first try, retries, JSON re-asks) and
send what it returns. Leak plugs the egress guard and the budget in there, so a
retry can never skip redaction or spend past the ceiling.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    """The model asking us to run a tool."""

    id: str
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    # Opaque provider data that must be echoed back on the next turn
    # (OpenRouter `reasoning_details`, which carry e.g. Gemini 3 thought
    # signatures across tool turns). The harness never reads it.
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
    # What the gateway says it billed, in USD (OpenRouter `usage.cost`). None =
    # not reported; the budget then estimates from its price table, never 0.
    cost_usd: Decimal | None = None


class LLMResponse(BaseModel):
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    model: str
    finish_reason: str | None = None
    served_by: str | None = None  # upstream that ran it (OpenRouter `provider`), for audits


class ToolSpec(BaseModel):
    """A tool as the model sees it: name, description, JSON schema of args."""

    name: str
    description: str
    parameters: dict[str, Any]


class LLMError(RuntimeError):
    """Raised by providers after retries are exhausted.

    A failed call always raises: no provider may turn a failure into an empty
    success. Messages must never contain prompts, keys or auth headers.
    `spent` is usage already incurred before the failure (e.g. a JSON re-ask
    that failed), so callers can still charge it to the budget.
    """

    def __init__(self, message: str = "", *, spent: Usage | None = None) -> None:
        super().__init__(message)
        self.spent = spent


# (attempt, system, messages) -> (system, messages) to actually send. attempt 0
# is the first try; 1.. are retries and re-asks. May raise to block the send.
BeforeRequest = Callable[[int, str, list[Message]], tuple[str, list[Message]]]


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
        before_request: BeforeRequest | None = None,
    ) -> LLMResponse: ...
