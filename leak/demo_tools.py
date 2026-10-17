"""Toy tools used by tests and `leak demo-harness` to exercise the harness
without any real data. One per permission level, plus a plain read tool."""

from __future__ import annotations

from pydantic import BaseModel, Field

from leak.harness.tools import ToolRegistry


class AddArgs(BaseModel):
    a: int
    b: int


class WeatherArgs(BaseModel):
    city: str = Field(description="City name, e.g. 'Bengaluru'")


class DraftArgs(BaseModel):
    to: str = Field(description="Recipient email address")
    body: str = ""


class NoArgs(BaseModel):
    pass


def build_toy_registry(calls: list[str] | None = None) -> ToolRegistry:
    """`calls` (optional) records every tool function that actually ran, so
    tests can prove that queued/forbidden tools were never executed."""
    ran = calls if calls is not None else []
    reg = ToolRegistry()

    @reg.tool(description="Add two integers.")
    def add(args: AddArgs) -> int:
        ran.append("add")
        return args.a + args.b

    @reg.tool(description="Get (fake) current weather for a city.")
    def get_weather(args: WeatherArgs) -> dict[str, str]:
        ran.append("get_weather")
        return {"city": args.city, "forecast": "29°C, humid, chance of rain"}

    @reg.tool(description="Draft a greeting email. Requires user approval.",
              permission="needs_approval")  # fmt: skip
    def send_greeting_draft(args: DraftArgs) -> str:
        ran.append("send_greeting_draft")
        return f"draft created for {args.to}"

    @reg.tool(description="Delete everything.", permission="forbidden")
    def rm_rf(args: NoArgs) -> str:
        ran.append("rm_rf")
        return "deleted"

    return reg
