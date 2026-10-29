import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2 as httpx
import keyring.errors
from keyring.backend import KeyringBackend

from leak.demo_tools import build_toy_registry
from leak.harness import AgentLoop, Budget, PermissionPolicy
from leak.llm.fake import Script, ScriptedProvider
from leak.llm.provider import LLMResponse, ToolCall, Usage

MODEL = "test-model"


def resp(
    text: str = "",
    calls: list[ToolCall] | None = None,
    inp: int = 100,
    out: int = 10,
    cached: int = 0,
) -> LLMResponse:
    return LLMResponse(
        model=MODEL,
        text=text,
        tool_calls=calls or [],
        usage=Usage(input_tokens=inp, output_tokens=out, cached_tokens=cached),
    )


def call(name: str, i: str = "c1", **args: object) -> ToolCall:
    return ToolCall(id=i, name=name, args=dict(args))


def make_loop(
    script: Script,
    budget: Budget,
    tmp_path: Path,
    ran: list[str] | None = None,
    policy: PermissionPolicy | None = None,
) -> tuple[AgentLoop, ScriptedProvider]:
    provider = ScriptedProvider(script)
    loop = AgentLoop(
        provider=provider,
        model=MODEL,
        registry=build_toy_registry(ran),
        budget=budget,
        trace_dir=tmp_path,
        system="test",
        policy=policy,
    )
    return loop, provider


# --- an in-memory OS keychain -------------------------------------------------------


class MemoryKeyring(KeyringBackend):
    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.data: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()

    def get_password(self, service: str, username: str) -> str | None:
        with self._lock:
            return self.data.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        with self._lock:
            self.data[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        with self._lock:
            if self.data.pop((service, username), None) is None:
                raise keyring.errors.PasswordDeleteError("not found")


# --- a fake OpenRouter, at the HTTP layer -----------------------------------------------

Outcome = httpx.Response | Exception | Callable[[httpx.Request], httpx.Response]


def completion(
    text: str | None = "ok",
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    cost: float | None = 0.0012,
    prompt: int = 100,
    out: int = 20,
    cached: int = 0,
    reasoning_details: list[dict[str, Any]] | None = None,
    finish: str = "stop",
) -> httpx.Response:
    message: dict[str, Any] = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls
    if reasoning_details:
        message["reasoning_details"] = reasoning_details
    usage: dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": out,
        "total_tokens": prompt + out,
        "prompt_tokens_details": {"cached_tokens": cached},
    }
    if cost is not None:
        usage["cost"] = cost
    return httpx.Response(
        200,
        json={
            "id": "gen-1",
            "object": "chat.completion",
            "created": 1,
            "model": "google/gemini-3.5-flash-lite",
            "provider": "Google",
            "choices": [{"index": 0, "finish_reason": finish, "message": message}],
            "usage": usage,
        },
    )


def tool_call(name: str, args: dict[str, Any], i: str = "call_1") -> dict[str, Any]:
    return {"id": i, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def error(status: int, message: str = "boom", headers: dict[str, str] | None = None):
    return httpx.Response(
        status, json={"error": {"code": status, "message": message}}, headers=headers or {}
    )


class FakeOpenRouter:
    """MockTransport that plays back outcomes and records every request."""

    def __init__(self, outcomes: list[Outcome] | Callable[[httpx.Request], httpx.Response]):
        self.outcomes = outcomes
        self.requests: list[httpx.Request] = []
        self._lock = threading.Lock()
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self.requests.append(request)
            if callable(self.outcomes):
                return self.outcomes(request)
            outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            return outcome(request)
        return outcome

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests if r.content]
