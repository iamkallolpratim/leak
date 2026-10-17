"""The agent loop: plan → tool call → observe → repeat.

This is the whole "agent". Strip away the comments and it is ~60 lines:

    while True:
        budget.start_step()                    # may raise BudgetExceeded
        response = llm.generate(context)       # model thinks, maybe asks for tools
        budget.charge(response.usage)          # may raise BudgetExceeded
        if no tool calls: return response.text # model is done
        for each tool call:
            auto           -> run it, feed result back
            needs_approval -> queue it, tell model it was queued (NOT run)
            forbidden      -> refuse, tell model
        context.compact()

Everything that could make an agent dangerous or expensive is enforced
*here*, in code, not by asking the model nicely in a prompt.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from leak.harness.budget import Budget, BudgetExceeded
from leak.harness.context import Context
from leak.harness.permissions import ApprovalQueue, PendingAction, PermissionPolicy
from leak.harness.tools import ToolRegistry
from leak.harness.trace import Tracer
from leak.llm.provider import LLMError, LLMProvider, Message, ToolCall, ToolResult

RunStatus = Literal["completed", "budget_exceeded", "error"]

# Last hook before a payload leaves the process: (system, messages) -> same,
# filtered. Leak plugs its PII guard in here; it may raise LLMError to block.
EgressFilter = Callable[[str, list[Message]], tuple[str, list[Message]]]


@dataclass
class RunResult:
    run_id: str
    status: RunStatus
    final_text: str = ""
    reason: str | None = None
    pending_actions: list[PendingAction] = field(default_factory=list)
    steps: int = 0
    trace_path: Path | None = None


class AgentLoop:
    def __init__(
        self,
        *,
        provider: LLMProvider,
        model: str,
        registry: ToolRegistry,
        budget: Budget,
        trace_dir: Path,
        system: str,
        policy: PermissionPolicy | None = None,
        queue: ApprovalQueue | None = None,
        run_id: str | None = None,
        egress: EgressFilter | None = None,
        trace_scrub: Callable[[Any], Any] | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.registry = registry
        self.budget = budget
        self.policy = policy or PermissionPolicy()
        self.queue = queue or ApprovalQueue()
        self.run_id = run_id or "run_" + uuid.uuid4().hex[:12]
        self.tracer = Tracer(self.run_id, trace_dir, scrub=trace_scrub)
        self.context = Context(system)
        self.egress = egress
        self._queued: list[PendingAction] = []

    # -- public ---------------------------------------------------------------
    def run(self, prompt: str) -> RunResult:
        self.tracer.emit(
            "run_start",
            0,
            prompt=prompt,
            model=self.model,
            provider=self.provider.name,
            max_steps=self.budget.max_steps,
            max_usd=str(self.budget.max_usd),
            tools=[s.name for s in self.registry.specs()],
        )
        self.context.add_user(prompt)
        try:
            text = self._loop()
            return self._finish("completed", final_text=text)
        except BudgetExceeded as e:
            return self._finish("budget_exceeded", reason=e.reason)
        except LLMError as e:
            return self._finish("error", reason=str(e))

    # -- the loop ---------------------------------------------------------------
    def _loop(self) -> str:
        while True:
            # 1. Ask permission to take another step.
            self.budget.start_step()
            step = self.budget.steps

            # 2. Plan: let the model look at the context and decide.
            #    The egress filter runs first, and we trace its *output*:
            #    the trace records exactly what left the machine.
            tools = self.registry.specs()
            system, messages = self.context.system, self.context.messages
            if self.egress:
                system, messages = self.egress(system, messages)
            self.tracer.emit(
                "llm_request",
                step,
                n_messages=len(messages),
                system=system,
                messages=[m.model_dump(mode="json") for m in messages],
                tools=[t.name for t in tools],
            )
            response = self.provider.generate(
                model=self.model,
                system=system,
                messages=messages,
                tools=tools,
            )

            # 3. Pay for it. Trace *before* charging so an over-budget call
            #    still appears in the log with its cost.
            cost = self.budget.cost_of(response.usage, response.model or self.model)
            self.tracer.emit(
                "llm_response",
                step,
                response=response.model_dump(mode="json"),
                cost_usd=f"{cost:.6f}",
                cost_inr=f"{self.budget.to_inr(cost):.4f}",
            )
            self.budget.charge(response.usage, response.model or self.model)

            self.context.add(
                Message(role="assistant", text=response.text, tool_calls=response.tool_calls)
            )

            # 4. No tool calls means the model considers itself done.
            if not response.tool_calls:
                return response.text

            # 5. Act + observe: every call gets exactly one result back.
            results = [self._handle(call, step) for call in response.tool_calls]
            self.context.add(Message(role="tool", tool_results=results))

            # 6. Keep the prompt from growing without bound.
            self.context.compact()

    def _handle(self, call: ToolCall, step: int) -> ToolResult:
        tool = self.registry.get(call.name)
        permission = self.policy.decide(call.name, tool.permission) if tool else "auto"
        self.tracer.emit("tool_call", step, id=call.id, name=call.name, args=call.args,
                         permission=permission)  # fmt: skip

        if permission == "forbidden":
            result = ToolResult(call_id=call.id, name=call.name, is_error=True,
                                content=f"tool '{call.name}' is forbidden")  # fmt: skip
        elif permission == "needs_approval":
            # The key safety property: the function is NOT called here.
            action = self.queue.enqueue(self.run_id, call.name, call.args)
            self._queued.append(action)
            self.tracer.emit("approval_queued", step, action_id=action.id, name=call.name)
            result = ToolResult(
                call_id=call.id,
                name=call.name,
                content=f"Queued for user approval as action {action.id}. It has NOT "
                "been executed. Do not assume it succeeded; continue with other work.",
            )
        else:
            result = self.registry.call(call)  # unknown tools error out in here

        self.tracer.emit("tool_result", step, id=call.id, name=call.name,
                         content=result.content, is_error=result.is_error)  # fmt: skip
        return result

    def _finish(
        self, status: RunStatus, final_text: str = "", reason: str | None = None
    ) -> RunResult:
        self.tracer.emit(
            "run_end",
            self.budget.steps,
            status=status,
            reason=reason,
            final_text=final_text,
            budget=self.budget.snapshot(),
            pending_actions=[a.id for a in self._queued],
        )
        return RunResult(
            run_id=self.run_id,
            status=status,
            final_text=final_text,
            reason=reason,
            pending_actions=list(self._queued),
            steps=self.budget.steps,
            trace_path=self.tracer.path,
        )
