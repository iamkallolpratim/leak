"""Execute actions a human approved.

The agent loop can only *enqueue*. This module is the only code path that
turns an approved action into a side effect, and it runs only from an explicit
human approval (CLI `leak actions approve` or the web UI's Approve button).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from leak.actions.drafts import DraftEmail, compose, create_gmail_draft
from leak.actions.queue import SqliteApprovalQueue
from leak.actions.reminders import renewal_ics
from leak.config import Settings
from leak.findings import Finding
from leak.harness.permissions import PendingAction
from leak.ledger.merchants import default_catalog
from leak.privacy.guard import vet_outbound
from leak.store import Store

DRAFT = "draft_cancellation_email"
REMINDER = "create_renewal_reminder"
ACTION_TOOLS = (DRAFT, REMINDER)


class ActionError(Exception):
    pass


def gmail_compose_service(settings: Settings) -> Any:
    if not settings.gmail.enable_actions:
        raise ActionError(
            "Gmail drafts are off. Set `[gmail] enable_actions = true` in config.toml, then run "
            "`leak gmail auth` once to grant the drafts (gmail.compose) permission."
        )
    from leak.ingest.gmail import build_service, get_credentials

    creds = get_credentials(
        settings.gmail.client_secret.expanduser(), settings.gmail_token_path, enable_actions=True
    )
    return build_service(creds)


class Executor:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        *,
        demo: bool = False,
        gmail: Callable[[], Any] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.queue = SqliteApprovalQueue(store)
        self.demo = demo
        self._gmail = gmail or (lambda: gmail_compose_service(settings))
        self.out_dir = settings.paths.data_dir

    def _finding(self, action: PendingAction) -> Finding:
        f = self.store.get_finding(str(action.args.get("finding_id", "")))
        if f is None:
            raise ActionError(f"finding {action.args.get('finding_id')} no longer exists")
        return f

    def draft_for(self, finding: Finding) -> DraftEmail:
        return compose(
            finding,
            store=self.store,
            catalog=default_catalog(),
            sender_name=self._sender(),
            demo=self.demo,
        )

    def vet(self, action: PendingAction, finding: Finding) -> None:
        """Fail closed on generated content before it reaches Google or disk.

        The draft body is a template, but the merchant name and the reminder's
        explanation came from the model, which read untrusted email. Block the
        action if anything looks like personal data, a link, or header tricks.
        """
        if action.tool == DRAFT:
            d = self.draft_for(finding)
            # Your own name in the signature is intended; the generic scan
            # doesn't flag names, only identifiers (cards, PAN, phones, ...).
            texts = {"subject": d.subject, "body": d.body}
            untrusted = {"merchant": finding.merchant}
        else:
            texts = {}
            untrusted = {"merchant": finding.merchant, "explanation": finding.explanation}
        problems = vet_outbound(texts, untrusted=untrusted)
        if problems:
            raise ActionError("blocked by the outbound check: " + "; ".join(problems))

    def _sender(self) -> str:
        names = self.settings.privacy.my_names
        return names[0] if names else ""

    def preview(self, action: PendingAction) -> dict[str, Any]:
        """What approving would do -- computed and vetted, nothing executed."""
        f = self._finding(action)
        self.vet(action, f)
        if action.tool == DRAFT:
            return {"kind": "email", **self.draft_for(f).model_dump()}
        if action.tool == REMINDER:
            return {"kind": "ics", "ics": renewal_ics(f), "date": str(f.next_renewal)}
        raise ActionError(f"unknown action {action.tool}")

    def _run(self, action: PendingAction) -> str:
        f = self._finding(action)
        self.vet(action, f)  # last check, right before the Gmail call / file write
        if action.tool == DRAFT:
            draft = self.draft_for(f)
            local = self._write(f"drafts/{action.id}.eml", bytes(draft.to_mime()))
            draft_id = create_gmail_draft(self._gmail(), draft)
            return f"Gmail draft created (id {draft_id}); open Gmail → Drafts. Copy: {local}"
        if action.tool == REMINDER:
            path = self._write(f"reminders/{f.id}.ics", renewal_ics(f).encode())
            return (
                f"Reminder for {f.next_renewal} saved to {path}; open it to add to your calendar."
            )
        raise ActionError(f"unknown action {action.tool}")

    def _write(self, rel: str, data: bytes) -> Path:
        path = self.out_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def approve(self, action_id: str) -> PendingAction:
        action = self.queue.get(action_id)
        if action is None:
            raise ActionError(f"no action {action_id}")
        if action.status not in ("pending", "failed"):  # failed may be retried after a fix
            raise ActionError(f"action {action_id} is already {action.status}")
        self.queue.set_status(action_id, "approved")
        try:
            self.queue.set_status(action_id, "executed", self._run(action))
        except Exception as e:  # surface to the user, keep the action retryable
            self.queue.set_status(action_id, "failed", f"{type(e).__name__}: {e}")
        result = self.queue.get(action_id)
        assert result is not None
        return result

    def reject(self, action_id: str) -> PendingAction:
        action = self.queue.get(action_id)
        if action is None or action.status not in ("pending", "failed"):
            raise ActionError(f"action {action_id} cannot be rejected")
        self.queue.set_status(action_id, "rejected")
        result = self.queue.get(action_id)
        assert result is not None
        return result

    def request(self, finding_id: str, tool: str) -> PendingAction:
        """User-initiated (from the UI): queue, don't execute."""
        if tool not in ACTION_TOOLS:
            raise ActionError(f"unknown action {tool}")
        if self.store.get_finding(finding_id) is None:
            raise ActionError(f"no finding {finding_id}")
        return self.queue.enqueue("user", tool, {"finding_id": finding_id})
