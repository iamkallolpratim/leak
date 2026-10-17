"""Tool registry.

A *tool* is a plain Python function that takes exactly one Pydantic model as
its argument. The registry:

1. derives the JSON schema the LLM sees from that model (no hand-written
   schemas to drift out of sync with code),
2. validates the LLM's raw JSON arguments against the model before calling,
3. converts every outcome -- success, bad arguments, exceptions -- into a
   `ToolResult` the loop can feed back. Tools never crash the loop; the model
   sees the error and can correct itself.

    registry = ToolRegistry()

    class AddArgs(BaseModel):
        a: int
        b: int

    @registry.tool(description="Add two integers.")
    def add(args: AddArgs) -> int:
        return args.a + args.b
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, get_type_hints

from pydantic import BaseModel, ValidationError

from leak.harness.permissions import Permission
from leak.llm.provider import ToolCall, ToolResult, ToolSpec

ToolFn = Callable[[Any], Any]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    fn: ToolFn
    permission: Permission

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.description,
            parameters=json_schema_for(self.args_model),
        )


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def tool(
        self,
        *,
        description: str,
        permission: Permission = "auto",
        name: str | None = None,
    ) -> Callable[[ToolFn], ToolFn]:
        """Decorator that registers a function as a tool."""

        def register(fn: ToolFn) -> ToolFn:
            self.add(
                Tool(
                    name=name or fn.__name__,
                    description=description,
                    args_model=_args_model_of(fn),
                    fn=fn,
                    permission=permission,
                )
            )
            return fn

        return register

    def add(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def specs(self) -> list[ToolSpec]:
        """Schemas for every tool the model may *see*. Forbidden tools are
        hidden entirely -- the cheapest way to stop a call is to never offer it."""
        return [t.spec() for t in self._tools.values() if t.permission != "forbidden"]

    def call(self, call: ToolCall) -> ToolResult:
        """Validate args and run the tool. Never raises."""
        tool = self._tools.get(call.name)
        if tool is None:
            return _error(call, f"unknown tool '{call.name}'")
        try:
            args = tool.args_model.model_validate(call.args)
        except ValidationError as e:
            # Feed the exact validation error back; models fix these well.
            return _error(call, f"invalid arguments: {e.errors(include_url=False)}")
        try:
            output = tool.fn(args)
        except Exception as e:  # tool bugs become observations, not crashes
            return _error(call, f"{type(e).__name__}: {e}")
        if isinstance(output, BaseModel):
            output = output.model_dump(mode="json")
        return ToolResult(call_id=call.id, name=call.name, content=output)


def _error(call: ToolCall, msg: str) -> ToolResult:
    return ToolResult(call_id=call.id, name=call.name, content=msg, is_error=True)


def _args_model_of(fn: ToolFn) -> type[BaseModel]:
    params = list(inspect.signature(fn).parameters.values())
    hints = get_type_hints(fn)
    if len(params) != 1 or not (
        isinstance(model := hints.get(params[0].name), type) and issubclass(model, BaseModel)
    ):
        raise TypeError(f"tool {fn.__name__} must take exactly one Pydantic model argument")
    return model


# --- JSON schema -----------------------------------------------------------
# Pydantic emits `$defs` + `$ref` for nested models and adds `title` keys.
# LLM function-calling APIs accept only a subset of JSON Schema, so we inline
# references and drop the noise. This is the whole "schema generation" story.

_DROP_KEYS = {"title", "$defs"}


def json_schema_for(model: type[BaseModel]) -> dict[str, Any]:
    raw = model.model_json_schema()
    defs: dict[str, Any] = raw.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                target = defs[node["$ref"].rsplit("/", 1)[-1]]
                return walk({**target, **{k: v for k, v in node.items() if k != "$ref"}})
            out: dict[str, Any] = {}
            for k, v in node.items():
                if k == "properties":  # keys here are field names, keep them all
                    out[k] = {name: walk(sub) for name, sub in v.items()}
                elif k not in _DROP_KEYS:
                    out[k] = walk(v)
            return out
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    schema: dict[str, Any] = walk(raw)
    return schema
