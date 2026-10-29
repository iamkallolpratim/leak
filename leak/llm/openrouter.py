"""OpenRouter: the only gateway Leak uses for remote inference.

OpenRouter speaks the OpenAI chat-completions dialect, so this is a thin
adapter over the official `openai` SDK pointed at https://openrouter.ai/api/v1.
Responsibilities:

* translate neutral Messages/ToolSpecs <-> chat-completions JSON
* function calling (our loop runs the tools) and structured JSON output, with
  one re-ask when the JSON fails validation
* privacy routing on *every* HTTP attempt: `provider = {data_collection:
  "deny", zdr: true, require_parameters: true}`. It is a constant, not config,
  and it is never dropped or relaxed. If no endpoint satisfies it the call
  fails closed with `PrivacyRoutingUnavailable`.
* the per-attempt gate (`before_request`): egress guard + budget run before the
  first try, every retry and the JSON re-ask. No gate, no request.
* bounded exponential backoff with full jitter for 408/429/5xx/network errors,
  honouring `Retry-After`; never retrying 400/401/402/403/404
* errors mapped to `OpenRouterError` subclasses whose messages never carry
  the key, auth headers or prompt text
* usage as OpenRouter reports it (tokens, cache hits, billed `cost`)

The SDK's own retries are off (we count every attempt against the budget) and
its debug logging is silenced: at DEBUG it would log request bodies.

There is no streaming: nothing in Leak consumes partial output.

OpenRouter returns `reasoning_details` on assistant turns (for Gemini 3 these
carry the thought signatures). They must come back unchanged on the next turn
for tool calling to keep working, so we carry them, as JSON, in the first tool
call's `provider_meta`.
"""

from __future__ import annotations

import email.utils
import json
import logging
import math
import random
import re
import time
import uuid
from collections.abc import Callable, Iterable
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx2 as httpx  # the HTTP stack the openai SDK is built on
import openai
from pydantic import BaseModel, ValidationError

from leak.harness.tools import json_schema_for
from leak.llm.provider import (
    BeforeRequest,
    LLMError,
    LLMResponse,
    Message,
    ToolCall,
    ToolSpec,
    Usage,
)

BASE_URL = "https://openrouter.ai/api/v1"
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}

# The SDK logs full request options (prompts included) at DEBUG, e.g. with
# OPENAI_LOG=debug. Leak never wants that, whatever the environment says.
for _name in ("openai", "httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)


def privacy_routing() -> dict[str, Any]:
    """Sent as `provider` on every request. A fresh dict, so nobody can mutate it."""
    return {"data_collection": "deny", "zdr": True, "require_parameters": True}


# --- errors ----------------------------------------------------------------------------


class OpenRouterError(LLMError):
    kind = "error"

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
        spent: Usage | None = None,
    ) -> None:
        super().__init__(message, spent=spent)
        self.status = status
        self.retryable = retryable


class InvalidKey(OpenRouterError):
    kind = "invalid_key"


class InsufficientCredits(OpenRouterError):
    kind = "insufficient_credits"


class Forbidden(OpenRouterError):
    kind = "forbidden"


class RateLimited(OpenRouterError):
    kind = "rate_limited"


class RequestTimeout(OpenRouterError):
    kind = "timeout"


class ProviderUnavailable(OpenRouterError):
    kind = "unavailable"


class UnknownModel(OpenRouterError):
    kind = "unknown_model"


class ContextTooLong(OpenRouterError):
    kind = "context_too_long"


class MalformedOutput(OpenRouterError):
    kind = "malformed_output"


class PrivacyRoutingUnavailable(OpenRouterError):
    kind = "privacy_routing_unavailable"


class BadRequest(OpenRouterError):
    kind = "bad_request"


class MissingGate(OpenRouterError):
    kind = "missing_gate"


_SECRETS = re.compile(r"(?i)(sk-or-[A-Za-z0-9_\-]+|bearer\s+\S+)")


def safe_text(text: object, limit: int = 200) -> str:
    """Upstream error text, made safe to show and trace: no keys, one line, short."""
    s = _SECRETS.sub("[redacted]", " ".join(str(text).split()))
    return s if len(s) <= limit else s[: limit - 1] + "…"


def mask_key(key: str) -> str:
    key = key.strip()
    if len(key) < 16:
        return "****"
    prefix = "sk-or-v1-" if key.startswith("sk-or-v1-") else ""
    return f"{prefix}…{key[-4:]}"


_ROUTING_MARKERS = ("no endpoints", "data policy", "zdr", "zero data", "routing", "data collection")
_CONTEXT_MARKERS = (
    "context length",
    "context window",
    "maximum context",
    "context_length_exceeded",
    "too many tokens",
    "prompt is too long",
    "input is too long",
)


def classify(status: int | None, message: str, model: str) -> OpenRouterError:
    """Map an OpenRouter error (HTTP status or in-body error) to a typed LLMError."""
    msg = safe_text(message)
    low = msg.lower()
    routing_hint = (
        f"No OpenRouter endpoint for '{model}' satisfies Leak's privacy routing "
        "(zdr=true, data_collection=deny) together with the features this call needs "
        "(tools or structured output). Leak will not relax that. Pick a model with a "
        "compatible ZDR endpoint: `leak llm models` checks your configured ones."
    )
    if status == 401:
        return InvalidKey(
            "OpenRouter rejected the API key (401: invalid, disabled or revoked). "
            "Check it with `leak llm key validate`; replace it with `leak llm key replace`.",
            status=401,
        )
    if status == 402:
        return InsufficientCredits(
            "OpenRouter: insufficient credits or the key's spend limit was reached (402). "
            "Add credits or raise the key limit at https://openrouter.ai/settings; "
            "Leak does not retry this.",
            status=402,
        )
    if status == 403:
        return Forbidden(f"OpenRouter refused the request (403): {msg}", status=403)
    if status == 408:
        return RequestTimeout("OpenRouter timed out (408)", status=408, retryable=True)
    if status == 413 or any(m in low for m in _CONTEXT_MARKERS):
        return ContextTooLong(
            f"prompt is too long for '{model}' ({status}): {msg}. "
            "Lower [statements]/[agent] sizes or pick a model with a larger context.",
            status=status,
        )
    if status == 404 or "no endpoints" in low:
        if any(m in low for m in _ROUTING_MARKERS):
            return PrivacyRoutingUnavailable(f"{routing_hint} ({status}: {msg})", status=status)
        return UnknownModel(
            f"OpenRouter does not know model '{model}' ({status}: {msg}). Model ids look like "
            "'vendor/model'; see https://openrouter.ai/models and `leak llm models`.",
            status=status,
        )
    if status == 429:
        return RateLimited("OpenRouter rate limit (429)", status=429, retryable=True)
    if status == 503 and ("routing" in low or "no available provider" in low):
        # Retry (same constraints) in case it is a momentary outage; never loosen.
        return PrivacyRoutingUnavailable(f"{routing_hint} (503: {msg})", status=503, retryable=True)
    if status is not None and status >= 500:
        return ProviderUnavailable(
            f"OpenRouter or the upstream provider failed ({status}): {msg}",
            status=status,
            retryable=status in RETRYABLE_STATUS,
        )
    if (
        status == 400
        and "model" in low
        and any(
            m in low for m in ("not a valid model", "invalid model", "not found", "does not exist")
        )
    ):
        return UnknownModel(
            f"OpenRouter does not know model '{model}' (400: {msg}). See `leak llm models`.",
            status=400,
        )
    return BadRequest(f"OpenRouter rejected the request ({status}): {msg}", status=status)


def _error_message(body: object) -> str:
    if isinstance(body, dict):
        err = body.get("error", body)
        if isinstance(err, dict):
            return str(err.get("message") or "")
        return str(err)
    return str(body or "")


def retry_after_seconds(headers: httpx.Headers | None) -> float | None:
    """Seconds from a Retry-After header (delta-seconds or HTTP date)."""
    if headers is None:
        return None
    value = headers.get("retry-after")
    if not value:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(when.timestamp() - time.time(), 0.0)


# --- translation (module-level so tests can exercise it directly) -----------------------


def to_openai_messages(system: str, messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system}] if system else []
    for m in messages:
        if m.role == "user":
            out.append({"role": "user", "content": m.text})
        elif m.role == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": m.text or None}
            if m.tool_calls:
                msg["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": json.dumps(c.args)},
                    }
                    for c in m.tool_calls
                ]
                details = next(
                    (c.provider_meta["reasoning_details"] for c in m.tool_calls
                     if "reasoning_details" in c.provider_meta),
                    None,
                )  # fmt: skip
                if details:
                    msg["reasoning_details"] = json.loads(details)
            out.append(msg)
        else:  # tool results: one message per call, in order
            for r in m.tool_results:
                payload = {"error": r.content} if r.is_error else {"result": r.content}
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": r.call_id,
                        "content": json.dumps(payload, ensure_ascii=False, default=str),
                    }
                )
    return out


def build_request(
    model: str,
    system: str,
    messages: list[Message],
    tools: list[ToolSpec] | None,
    response_schema: type[BaseModel] | None,
) -> dict[str, Any]:
    """kwargs for `chat.completions.create`. Routing goes in `extra_body`."""
    body: dict[str, Any] = {
        "model": model,
        "messages": to_openai_messages(system, messages),
        "extra_body": {"provider": privacy_routing()},
    }
    if tools:
        body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in tools
        ]
    if response_schema is not None:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": response_schema.__name__,
                # Not strict: strict mode demands every field be required, which
                # our optional fields are not. We validate (and re-ask) ourselves.
                "strict": False,
                "schema": json_schema_for(response_schema),
            },
        }
    return body


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def parse_usage(raw: object, *, sent_chars: int, got_chars: int) -> Usage:
    if not isinstance(raw, dict):
        # No usage at all: estimate tokens (~3 chars each, rounded up) so the
        # budget still charges something. Unknown never means free.
        return Usage(input_tokens=-(-sent_chars // 3), output_tokens=-(-got_chars // 3))
    prompt_details = raw.get("prompt_tokens_details")
    cached = _int(prompt_details.get("cached_tokens")) if isinstance(prompt_details, dict) else 0
    cost: Decimal | None = None
    if raw.get("cost") is not None:
        try:
            cost = Decimal(str(raw["cost"]))
        except InvalidOperation:
            cost = None
        if cost is not None and (not cost.is_finite() or cost < 0):
            cost = None
    return Usage(
        input_tokens=_int(raw.get("prompt_tokens")),
        output_tokens=_int(raw.get("completion_tokens")),  # reasoning tokens included
        cached_tokens=cached,
        cost_usd=cost,
    )


def parse_response(data: dict[str, Any], model: str, *, sent_chars: int) -> LLMResponse:
    # A 200 can still carry an error (and no choices): never treat it as success.
    if data.get("error"):
        err = data["error"]
        code = err.get("code") if isinstance(err, dict) else None
        status = code if isinstance(code, int) else 502
        raise classify(status, _error_message(data), model)
    choices = data.get("choices") or []
    if not choices:
        raise MalformedOutput("OpenRouter returned no choices", retryable=True)
    choice = choices[0]
    finish = choice.get("finish_reason")
    if finish == "error":
        raise ProviderUnavailable("the upstream provider failed mid-response", retryable=True)
    message = choice.get("message") or {}
    content = message.get("content")
    text = content if isinstance(content, str) else ""

    calls: list[ToolCall] = []
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        name = str(fn.get("name") or "")
        raw_args = fn.get("arguments")
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) and raw_args else {}
        except json.JSONDecodeError:
            raise MalformedOutput(
                f"model sent arguments for tool '{safe_text(name, 60)}' that are not JSON"
            ) from None
        if not isinstance(args, dict):
            raise MalformedOutput(f"tool '{safe_text(name, 60)}' arguments are not an object")
        calls.append(
            ToolCall(id=str(tc.get("id") or "call_" + uuid.uuid4().hex[:8]), name=name, args=args)
        )
    details = message.get("reasoning_details")
    if calls and details:
        calls[0] = calls[0].model_copy(
            update={"provider_meta": {"reasoning_details": json.dumps(details)}}
        )

    if not calls and not text.strip():
        if finish == "length":
            raise MalformedOutput("the model hit its output limit before answering")
        if finish == "content_filter":
            raise Forbidden("the upstream provider's content filter blocked the answer")
        raise MalformedOutput("the model returned an empty response", retryable=True)

    usage = parse_usage(data.get("usage"), sent_chars=sent_chars, got_chars=len(text))
    served_by = data.get("provider")
    return LLMResponse(
        text=text,
        tool_calls=calls,
        usage=usage,
        model=model,
        finish_reason=str(finish) if finish else None,
        served_by=str(served_by) if served_by else None,
    )


def _add_usage(a: Usage, b: Usage) -> Usage:
    cost = None if a.cost_usd is None or b.cost_usd is None else a.cost_usd + b.cost_usd
    return Usage(
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        cached_tokens=a.cached_tokens + b.cached_tokens,
        cost_usd=cost,
    )


def _validation_summary(e: ValidationError) -> str:
    """Where the JSON was wrong, without echoing the values back."""
    rows = [
        f"- {'.'.join(str(p) for p in err['loc']) or '(root)'}: {err['msg']}"
        for err in e.errors(include_input=False, include_url=False)[:20]
    ]
    return "\n".join(rows)


# --- provider ---------------------------------------------------------------------------


class OpenRouterProvider:
    """One instance per credential. Build a new one when the key changes."""

    name = "openrouter"
    requires_egress_guard = True  # AgentLoop refuses to run it without one

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = 120.0,
        max_retries: int = 4,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        max_retry_after: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
        transport: httpx.BaseTransport | None = None,
        base_url: str = BASE_URL,
    ) -> None:
        if not api_key or not api_key.strip():
            raise InvalidKey("no OpenRouter API key: run `leak llm key set`")
        if not (math.isfinite(timeout) and timeout > 0):
            raise ValueError("timeout must be a finite number of seconds > 0")
        t = httpx.Timeout(timeout, connect=min(10.0, timeout))
        self._client = openai.OpenAI(
            api_key=api_key.strip(),
            base_url=base_url,
            max_retries=0,  # we retry ourselves, and count each attempt
            timeout=t,
            http_client=httpx.Client(transport=transport, timeout=t) if transport else None,
        )
        # Don't forward OPENAI_ORG_ID / OPENAI_PROJECT_ID from the environment.
        self._client.organization = None
        self._client.project = None
        self.key_hint = mask_key(api_key)
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.max_retry_after = max_retry_after
        self._sleep = sleep

    def __repr__(self) -> str:
        return f"OpenRouterProvider(key={self.key_hint})"

    def close(self) -> None:
        self._client.close()

    # -- public ---------------------------------------------------------------
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
        if before_request is None:
            raise MissingGate(
                "refusing to call OpenRouter without the egress gate "
                "(wrap the provider in GuardedProvider or give AgentLoop an egress filter)"
            )
        response, attempt = self._call(
            model, system, messages, tools, response_schema, before_request, 0
        )
        # Structured output: validate, and on failure re-ask exactly once with
        # where it went wrong. The re-ask goes through the gate like any request.
        if response_schema is not None and not response.tool_calls:
            try:
                response_schema.model_validate_json(response.text)
            except ValidationError as e:
                retry = [
                    *messages,
                    Message(role="assistant", text=response.text),
                    Message(role="user", text="That JSON failed validation:\n"
                            f"{_validation_summary(e)}\nReply again with corrected JSON only."),
                ]  # fmt: skip
                try:
                    second, _ = self._call(
                        model, system, retry, tools, response_schema, before_request, attempt
                    )
                except LLMError as err:
                    err.spent = _add_usage(response.usage, err.spent or Usage())
                    raise
                second.usage = _add_usage(response.usage, second.usage)
                response = second
                try:
                    response_schema.model_validate_json(response.text)
                except ValidationError as e2:
                    raise MalformedOutput(
                        "model returned invalid JSON twice:\n" + _validation_summary(e2),
                        spent=response.usage,
                    ) from None
        return response

    def key_info(self) -> dict[str, Any]:
        """GET /key: label, limits, usage of *this* key. Sends no prompt or data."""
        try:
            r = self._client.get("/key", cast_to=httpx.Response)
        except openai.APIStatusError as e:
            raise classify(e.status_code, _error_message(e.body), "-") from None
        except openai.APITimeoutError:
            raise RequestTimeout("OpenRouter timed out", retryable=True) from None
        except openai.APIConnectionError:
            raise ProviderUnavailable("could not reach OpenRouter", retryable=True) from None
        data = r.json().get("data")
        if not isinstance(data, dict):
            raise MalformedOutput("unexpected /key response")
        return data

    # -- internals --------------------------------------------------------------
    def _call(
        self,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None,
        response_schema: type[BaseModel] | None,
        gate: BeforeRequest,
        attempt: int,
    ) -> tuple[LLMResponse, int]:
        for retry in range(self.max_retries + 1):
            # Outside the try: a PrivacyViolation or BudgetExceeded from the
            # gate must stop us cold, before any byte leaves the machine.
            clean_system, clean_messages = gate(attempt, system, messages)
            attempt += 1
            body = build_request(model, clean_system, clean_messages, tools, response_schema)
            sent_chars = sum(len(json.dumps(m, default=str)) for m in body["messages"])
            retry_after: float | None = None
            try:
                raw = self._client.chat.completions.create(**body)
                return parse_response(raw.to_dict(), model, sent_chars=sent_chars), attempt
            except openai.APIStatusError as e:
                err = classify(e.status_code, _error_message(e.body), model)
                retry_after = retry_after_seconds(e.response.headers)
            except openai.APITimeoutError:
                err = RequestTimeout("OpenRouter request timed out", retryable=True)
            except openai.APIConnectionError:
                err = ProviderUnavailable("network error talking to OpenRouter", retryable=True)
            except OpenRouterError as e:
                err = e
            if not err.retryable or retry == self.max_retries:
                raise err
            delay = self._delay(retry, retry_after)
            if delay is None:  # server asked us to wait longer than we are willing to
                raise err
            self._sleep(delay)
        raise AssertionError("unreachable")

    def _delay(self, retry: int, retry_after: float | None) -> float | None:
        if retry_after is not None:
            if retry_after > self.max_retry_after:
                return None
            return retry_after + random.uniform(0, self.base_delay)
        # Exponential backoff with full jitter: up to 1s, 2s, 4s, 8s… capped.
        return random.uniform(0, min(self.max_delay, self.base_delay * 2**retry))


# --- model compatibility -------------------------------------------------------------

# What each role sends, hence what a ZDR endpoint must support (require_parameters).
ROLE_NEEDS: dict[str, set[str]] = {"extract": {"response_format"}, "reason": {"tools"}}


def fetch_catalog(
    transport: httpx.BaseTransport | None = None, base_url: str = BASE_URL
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Public model list + ZDR endpoint list. No key, no user data is sent."""
    with httpx.Client(transport=transport, timeout=httpx.Timeout(30.0)) as c:
        try:
            models = c.get(f"{base_url}/models").raise_for_status().json()["data"]
            zdr = c.get(f"{base_url}/endpoints/zdr").raise_for_status().json()["data"]
        except (httpx.HTTPError, KeyError, ValueError) as e:
            raise ProviderUnavailable(
                f"could not read OpenRouter's catalog: {safe_text(e)}"
            ) from None
    return list(models), list(zdr)


def check_model(
    model: str,
    needs: Iterable[str],
    models: list[dict[str, Any]],
    zdr: list[dict[str, Any]],
) -> list[str]:
    """Problems with using `model` under Leak's routing. Empty list = compatible."""
    needs = set(needs)
    if "/" not in model:
        return [f"'{model}' is not an OpenRouter id (they look like 'vendor/model')"]
    if model not in {m.get("id") for m in models}:
        return [f"'{model}' is not in OpenRouter's model catalog"]
    endpoints = [e for e in zdr if e.get("model_id") == model]
    if not endpoints:
        return [f"'{model}' has no Zero Data Retention endpoint"]
    for e in endpoints:
        params = set(e.get("supported_parameters") or [])
        if "structured_outputs" in params:
            params.add("response_format")
        if needs <= params:
            return []
    return [f"no ZDR endpoint of '{model}' supports {', '.join(sorted(needs))}"]
