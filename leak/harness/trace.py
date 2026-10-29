"""Tracing: an append-only JSONL log of everything a run did.

One file per run: `<data_dir>/traces/<run_id>.jsonl`. One JSON object per
line, written and flushed immediately, so a crashed run still leaves a usable
trace. Event types:

    run_start       prompt, model, budget limits
    llm_request     the exact messages + tool names sent (what the LLM saw)
    llm_response    text, tool calls, token usage, cost
    tool_call       a call the harness is about to handle + effective permission
    tool_result     what was fed back to the model
    approval_queued a needs_approval call parked for a human
    run_end         final status, final text, budget snapshot

Two kinds of replay:

* `render_replay(events)` -- pretty-print a past run step by step.
* `ReplayProvider(events)` -- an LLMProvider that returns the recorded
  responses in order, so the *loop itself* can be re-executed deterministically
  with no network and no cost (great for debugging and regression tests).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from leak.llm.provider import BeforeRequest, LLMError, LLMResponse, Message, ToolSpec


class TraceEvent(BaseModel):
    run_id: str
    seq: int
    ts: datetime
    type: str
    step: int
    data: dict[str, Any]


class Tracer:
    """`scrub` (optional) is applied to every event's data before writing, so
    traces stay safe to share even when a tool returned something sensitive."""

    def __init__(
        self, run_id: str, trace_dir: Path, scrub: Callable[[Any], Any] | None = None
    ) -> None:
        self.run_id = run_id
        trace_dir.mkdir(parents=True, exist_ok=True)
        self.path = trace_dir / f"{run_id}.jsonl"
        self.scrub = scrub
        self._seq = 0

    def emit(self, type: str, step: int, **data: Any) -> None:
        if self.scrub:
            data = self.scrub(data)
        self._seq += 1
        event = TraceEvent(
            run_id=self.run_id,
            seq=self._seq,
            ts=datetime.now(UTC),
            type=type,
            step=step,
            data=data,
        )
        with self.path.open("a", encoding="utf-8") as f:
            f.write(event.model_dump_json() + "\n")


def load_trace(path: Path) -> list[TraceEvent]:
    with path.open(encoding="utf-8") as f:
        return [TraceEvent.model_validate_json(line) for line in f if line.strip()]


def trace_path(trace_dir: Path, run_id: str) -> Path:
    return trace_dir / f"{run_id}.jsonl"


def list_runs(trace_dir: Path) -> list[Path]:
    return sorted(trace_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)


class ReplayProvider:
    """Feeds recorded `llm_response` events back, in order."""

    name = "replay"

    def __init__(self, events: list[TraceEvent]) -> None:
        self._responses: Iterator[dict[str, Any]] = iter(
            e.data["response"] for e in events if e.type == "llm_response"
        )

    def generate(
        self,
        *,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        response_schema: type[BaseModel] | None = None,
        before_request: BeforeRequest | None = None,  # replay sends nothing anywhere
    ) -> LLMResponse:
        try:
            return LLMResponse.model_validate(next(self._responses))
        except StopIteration:
            raise LLMError("replay exhausted: run asked for more steps than recorded") from None


# --- pretty printer ----------------------------------------------------------


def _short(value: Any, limit: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_replay(events: list[TraceEvent]) -> str:
    """Human-readable, step-by-step story of a run."""
    out: list[str] = []
    for e in events:
        d = e.data
        if e.type == "run_start":
            out.append(f"run {e.run_id}  model={d['model']}  provider={d['provider']}")
            out.append(f"  budget: {d['max_steps']} steps, ${d['max_usd']}")
            out.append(f"  prompt: {_short(d['prompt'])}")
        elif e.type == "llm_request":
            out.append(f"\n── step {e.step} ──  sending {d['n_messages']} messages")
        elif e.type == "llm_response":
            r, u = d["response"], d["response"]["usage"]
            out.append(
                f"  model: {u['input_tokens']} in / {u['output_tokens']} out tokens, "
                f"${d['cost_usd']} (₹{d['cost_inr']})"
            )
            if r["text"]:
                out.append(f"  says: {_short(r['text'])}")
        elif e.type == "tool_call":
            out.append(f"  → {d['name']}({_short(d['args'], 100)})  [{d['permission']}]")
        elif e.type == "tool_result":
            mark = "✗" if d["is_error"] else "←"
            out.append(f"    {mark} {_short(d['content'])}")
        elif e.type == "approval_queued":
            out.append(f"    ⏸ queued for approval as {d['action_id']} (not executed)")
        elif e.type == "run_end":
            b = d["budget"]
            out.append(f"\n══ {d['status']} ══  {d.get('reason') or ''}".rstrip())
            if d.get("final_text"):
                out.append(f"  final: {_short(d['final_text'], 400)}")
            out.append(
                f"  totals: {b['steps']} steps, {b['input_tokens']} in / "
                f"{b['output_tokens']} out tokens, ${b['spent_usd']} (₹{b['spent_inr']})"
            )
            if d.get("pending_actions"):
                out.append(f"  awaiting approval: {', '.join(d['pending_actions'])}")
    return "\n".join(out)
