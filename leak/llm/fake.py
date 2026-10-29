"""ScriptedProvider: a deterministic fake LLM for tests and offline demos.

Give it a list of responses (or a function of the conversation) and it plays
them back. No network, no key, no cost -- but the harness can't tell the
difference, which is exactly the point of the LLMProvider protocol.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from pydantic import BaseModel

from leak.llm.provider import BeforeRequest, LLMError, LLMResponse, Message, ToolSpec

Script = Sequence[LLMResponse] | Callable[[list[Message]], LLMResponse]


class ScriptedProvider:
    name = "scripted"

    def __init__(self, script: Script) -> None:
        self._script = script
        self._i = 0
        self.requests: list[list[Message]] = []  # what the "model" saw, per call
        self.systems: list[str] = []

    def generate(
        self,
        *,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        response_schema: type[BaseModel] | None = None,
        before_request: BeforeRequest | None = None,
    ) -> LLMResponse:
        # Behave like a network provider: the gate runs (and may block) first.
        if before_request is not None:
            system, messages = before_request(0, system, messages)
        self.systems.append(system)
        self.requests.append([m.model_copy(deep=True) for m in messages])
        if callable(self._script):
            return self._script(messages)
        if self._i >= len(self._script):
            raise LLMError("scripted provider ran out of responses")
        resp = self._script[self._i]
        self._i += 1
        return resp
