from pathlib import Path

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
