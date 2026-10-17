"""Leak's hand-built agent harness. Read in this order:

1. tools.py        -- how a Python function becomes something an LLM can call
2. permissions.py  -- auto / needs_approval / forbidden, and the approval queue
3. budget.py       -- step and money ceilings
4. context.py      -- what the model sees each step
5. trace.py        -- the JSONL flight recorder + replay
6. loop.py         -- the loop that ties it all together
"""

from leak.harness.budget import Budget, BudgetExceeded, ModelPrice
from leak.harness.loop import AgentLoop, RunResult
from leak.harness.permissions import ApprovalQueue, PendingAction, Permission, PermissionPolicy
from leak.harness.tools import Tool, ToolRegistry, json_schema_for
from leak.harness.trace import ReplayProvider, Tracer, load_trace, render_replay

__all__ = [
    "AgentLoop",
    "ApprovalQueue",
    "Budget",
    "BudgetExceeded",
    "ModelPrice",
    "PendingAction",
    "Permission",
    "PermissionPolicy",
    "ReplayProvider",
    "RunResult",
    "Tool",
    "ToolRegistry",
    "Tracer",
    "json_schema_for",
    "load_trace",
    "render_replay",
]
