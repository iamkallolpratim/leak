"""Permissions and the approval queue.

Every tool declares one of three permission levels:

    auto            run immediately (read-only things: query, search, read)
    needs_approval  do NOT run; park the call in the approval queue for a human
    forbidden       never run, never even shown to the model

The policy is the single place this decision is made, and config can tighten
a tool's level (e.g. make a read tool need approval) but never loosen
`forbidden`. When a call is queued the model is told so honestly -- it must
not believe an email draft exists when it doesn't.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Permission = Literal["auto", "needs_approval", "forbidden"]

# Ordering used to decide whether an override tightens or loosens.
_STRICTNESS: dict[Permission, int] = {"auto": 0, "needs_approval": 1, "forbidden": 2}


class PermissionPolicy:
    def __init__(self, overrides: dict[str, Permission] | None = None) -> None:
        self.overrides = overrides or {}

    def decide(self, tool_name: str, declared: Permission) -> Permission:
        """Effective permission = the stricter of declared and override."""
        override = self.overrides.get(tool_name)
        if override is None:
            return declared
        return max(declared, override, key=_STRICTNESS.__getitem__)


ActionStatus = Literal["pending", "approved", "rejected", "executed", "failed"]


class PendingAction(BaseModel):
    id: str = Field(default_factory=lambda: "act_" + uuid.uuid4().hex[:10])
    run_id: str
    tool: str
    args: dict[str, Any]
    status: ActionStatus = "pending"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    result: str | None = None  # what executing it produced, or why it failed


class ApprovalQueue:
    """In-memory queue. Leak uses a SQLite-backed subclass (leak/actions/queue.py)
    with the same methods, so the loop doesn't change. Executing an approved
    action is the application's job, not the loop's: the loop only ever enqueues."""

    def __init__(self) -> None:
        self._items: dict[str, PendingAction] = {}

    def enqueue(self, run_id: str, tool: str, args: dict[str, Any]) -> PendingAction:
        action = PendingAction(run_id=run_id, tool=tool, args=args)
        self._items[action.id] = action
        return action

    def pending(self) -> list[PendingAction]:
        return [a for a in self._items.values() if a.status == "pending"]

    def get(self, action_id: str) -> PendingAction | None:
        return self._items.get(action_id)

    def set_status(self, action_id: str, status: ActionStatus, result: str | None = None) -> None:
        self._items[action_id].status = status
        self._items[action_id].result = result
