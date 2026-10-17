"""SQLite-backed approval queue: survives restarts, shared by CLI, agent and web UI."""

from __future__ import annotations

from typing import Any

from leak.harness.permissions import ActionStatus, ApprovalQueue, PendingAction
from leak.store import Store


class SqliteApprovalQueue(ApprovalQueue):
    def __init__(self, store: Store) -> None:
        self.table = store.db.table("actions")
        if not self.table.exists():
            self.table.create({"id": str, "status": str, "created_at": str, "data": str}, pk="id")

    def _save(self, a: PendingAction) -> None:
        self.table.upsert(
            {
                "id": a.id,
                "status": a.status,
                "created_at": a.created_at.isoformat(),
                "data": a.model_dump_json(),
            },
            pk="id",
        )

    def enqueue(self, run_id: str, tool: str, args: dict[str, Any]) -> PendingAction:
        # Same action for the same finding already waiting? Don't queue it twice.
        for a in self.pending():
            if a.tool == tool and a.args == args:
                return a
        action = PendingAction(run_id=run_id, tool=tool, args=args)
        self._save(action)
        return action

    def all(self) -> list[PendingAction]:
        rows = self.table.rows_where(order_by="created_at desc")
        return [PendingAction.model_validate_json(r["data"]) for r in rows]

    def pending(self) -> list[PendingAction]:
        return [a for a in self.all() if a.status == "pending"]

    def get(self, action_id: str) -> PendingAction | None:
        rows = list(self.table.rows_where("id = ?", [action_id]))
        return PendingAction.model_validate_json(rows[0]["data"]) if rows else None

    def set_status(self, action_id: str, status: ActionStatus, result: str | None = None) -> None:
        a = self.get(action_id)
        if a is None:
            raise KeyError(action_id)
        self._save(a.model_copy(update={"status": status, "result": result}))
