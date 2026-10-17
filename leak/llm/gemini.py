"""Gemini provider built on the official `google-genai` SDK.

Responsibilities:
* translate neutral Messages/ToolSpecs <-> genai Content/FunctionDeclaration
* function calling with *automatic* calling disabled (our loop runs tools)
* structured output (JSON schema) with one re-ask on validation failure
* retry with exponential backoff + jitter on rate limits / server errors
* report token usage so the budget can price the call

Gemini 3 models attach a `thought_signature` to function-call parts that must
be sent back verbatim on the next turn; we carry it in ToolCall.provider_meta.
"""

from __future__ import annotations

import base64
import random
import time
import uuid
from collections.abc import Callable
from typing import Any

import httpx
from google import genai
from google.genai import errors, types
from pydantic import BaseModel, ValidationError

from leak.harness.tools import json_schema_for
from leak.llm.provider import LLMError, LLMResponse, Message, ToolCall, ToolSpec, Usage

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class GeminiProvider:
    name = "gemini"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        client: Any | None = None,
        max_retries: int = 4,
        base_delay: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if client is None:
            if not api_key:
                raise LLMError("GEMINI_API_KEY is not set (put it in .env)")
            client = genai.Client(api_key=api_key)
        self.client: Any = client
        self.max_retries = max_retries
        self.base_delay = base_delay
        self._sleep = sleep

    # -- public ---------------------------------------------------------------
    def generate(
        self,
        *,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        response_schema: type[BaseModel] | None = None,
    ) -> LLMResponse:
        contents = [to_content(m) for m in messages]
        config = self._config(system, tools, response_schema)
        response = self._call_with_retry(model, contents, config)

        # Structured output: validate, and on failure re-ask exactly once with
        # the validation error. Models usually fix their JSON when told how.
        if response_schema is not None and not response.tool_calls:
            try:
                response_schema.model_validate_json(response.text)
            except ValidationError as e:
                retry_contents = [
                    *contents,
                    types.Content(role="model", parts=[types.Part(text=response.text)]),
                    types.Content(
                        role="user",
                        parts=[types.Part(text=f"That JSON failed validation:\n{e}\n"
                                               "Reply again with corrected JSON only.")],
                    ),
                ]  # fmt: skip
                second = self._call_with_retry(model, retry_contents, config)
                second.usage = _add_usage(response.usage, second.usage)
                response = second
                try:
                    response_schema.model_validate_json(response.text)
                except ValidationError as e2:
                    raise LLMError(f"model returned invalid JSON twice: {e2}") from e2
        return response

    # -- internals --------------------------------------------------------------
    def _config(
        self,
        system: str,
        tools: list[ToolSpec] | None,
        response_schema: type[BaseModel] | None,
    ) -> types.GenerateContentConfig:
        kwargs: dict[str, Any] = {
            "system_instruction": system or None,
            "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True),
        }
        if tools:
            kwargs["tools"] = [
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=t.name,
                            description=t.description,
                            parameters_json_schema=t.parameters,
                        )
                        for t in tools
                    ]
                )
            ]
        if response_schema is not None:
            kwargs["response_mime_type"] = "application/json"
            kwargs["response_json_schema"] = json_schema_for(response_schema)
        return types.GenerateContentConfig(**kwargs)

    def _call_with_retry(
        self, model: str, contents: list[types.Content], config: types.GenerateContentConfig
    ) -> LLMResponse:
        for attempt in range(self.max_retries + 1):
            try:
                raw = self.client.models.generate_content(
                    model=model, contents=contents, config=config
                )
                return from_response(raw, model)
            except errors.APIError as e:
                if e.code not in RETRYABLE_STATUS or attempt == self.max_retries:
                    raise LLMError(f"Gemini API error {e.code}: {e.message}") from e
            except httpx.TransportError as e:
                if attempt == self.max_retries:
                    raise LLMError(f"network error talking to Gemini: {e}") from e
            # Exponential backoff with full jitter: 1s, 2s, 4s, 8s (randomized).
            self._sleep(random.uniform(0, self.base_delay * 2**attempt))
        raise AssertionError("unreachable")


# --- translation (module-level so tests can exercise it directly) -------------


def to_content(m: Message) -> types.Content:
    if m.role == "user":
        return types.Content(role="user", parts=[types.Part(text=m.text)])
    if m.role == "assistant":
        parts: list[types.Part] = []
        if m.text:
            parts.append(types.Part(text=m.text))
        for call in m.tool_calls:
            sig = call.provider_meta.get("thought_signature")
            parts.append(
                types.Part(
                    function_call=types.FunctionCall(id=call.id, name=call.name, args=call.args),
                    thought_signature=base64.b64decode(sig) if sig else None,
                )
            )
        return types.Content(role="model", parts=parts)
    # role == "tool": function responses go back as a user turn.
    return types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id=r.call_id,
                    name=r.name,
                    response={"error": r.content} if r.is_error else {"result": r.content},
                )
            )
            for r in m.tool_results
        ],
    )


def from_response(raw: types.GenerateContentResponse, model: str) -> LLMResponse:
    texts: list[str] = []
    calls: list[ToolCall] = []
    finish: str | None = None
    if raw.candidates:
        cand = raw.candidates[0]
        finish = str(cand.finish_reason) if cand.finish_reason else None
        for part in (cand.content.parts if cand.content else None) or []:
            if part.function_call:
                fc = part.function_call
                meta = {}
                if part.thought_signature:
                    meta["thought_signature"] = base64.b64encode(part.thought_signature).decode()
                calls.append(
                    ToolCall(
                        id=fc.id or "call_" + uuid.uuid4().hex[:8],
                        name=fc.name or "",
                        args=dict(fc.args or {}),
                        provider_meta=meta,
                    )
                )
            elif part.text and not part.thought:
                texts.append(part.text)
    um = raw.usage_metadata
    usage = Usage(
        input_tokens=(um.prompt_token_count or 0) if um else 0,
        output_tokens=((um.candidates_token_count or 0) + (um.thoughts_token_count or 0))
        if um
        else 0,
        cached_tokens=(um.cached_content_token_count or 0) if um else 0,
    )
    return LLMResponse(
        text="".join(texts), tool_calls=calls, usage=usage, model=model, finish_reason=finish
    )


def _add_usage(a: Usage, b: Usage) -> Usage:
    return Usage(
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        cached_tokens=a.cached_tokens + b.cached_tokens,
    )
