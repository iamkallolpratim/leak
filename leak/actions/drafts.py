"""Cancellation / refund email drafts.

Drafts are written from a template, not by the LLM: they go out under the
user's name, so they should say exactly what the evidence supports and
nothing more. Leak creates a Gmail *draft* (gmail.compose scope); there is no
send code anywhere in this repository.
"""

from __future__ import annotations

import base64
from datetime import date
from email.message import EmailMessage
from typing import Any

from pydantic import BaseModel

from leak.findings import Finding
from leak.ledger.merchants import Catalog
from leak.store import Store

DEMO_PREFIX = "[LEAK DEMO - do not send] "


class DraftEmail(BaseModel):
    to: str
    subject: str
    body: str
    cancel_url: str | None = None  # shown to the user alongside the draft

    def to_mime(self) -> EmailMessage:
        msg = EmailMessage()
        if self.to:
            msg["To"] = self.to
        msg["Subject"] = self.subject
        msg.set_content(self.body)
        return msg


def _charges_after(store: Store, merchant_id: str, after: date) -> list[dict[str, Any]]:
    return [
        r
        for r in store.ledger()
        if r["merchant_id"] == merchant_id
        and r["direction"] == "debit"
        and date.fromisoformat(r["txn_date"]) > after
    ]


def compose(
    finding: Finding, *, store: Store, catalog: Catalog, sender_name: str, demo: bool = False
) -> DraftEmail:
    # Agent-submitted findings may lack merchant_id; resolve by name so a refund
    # request never degrades into a generic "please cancel" email.
    m = (
        catalog.get(finding.merchant_id)
        if finding.merchant_id
        else catalog.resolve(finding.merchant)
    )
    merchant_id = m.id if m else finding.merchant_id
    name = m.name if m else finding.merchant
    to = "" if demo else (m.cancel.get("email", "") if m else "")
    sign = f"\n\nThanks,\n{sender_name}" if sender_name else "\n\nThanks"

    if finding.type == "zombie" and merchant_id:
        cancel = next(
            (
                e
                for e in store.events()
                if e["kind"] == "cancellation" and e["merchant_id"] == merchant_id
            ),
            None,
        )
        when = date.fromisoformat(cancel["event_date"]) if cancel and cancel["event_date"] else None
        charges = _charges_after(store, merchant_id, when) if when else []
        listed = "\n".join(f"  - {c['txn_date']}: Rs {c['amount_inr']:,.2f}" for c in charges)
        subject = f"Refund request: charged after cancelling {name}"
        body = (
            f"Hello {name} team,\n\nI cancelled my {name} subscription on {when:%d %B %Y}"
            if when
            else f"Hello {name} team,\n\nI cancelled my {name} subscription"
        )
        body += ", but I have continued to be charged afterwards"
        body += f":\n{listed}\n\n" if listed else ".\n\n"
        body += (
            "Please refund these charges and confirm that my subscription and any "
            "auto-debit mandate have been cancelled so that no further charges occur."
        )
    else:
        subject = f"Please cancel my {name} subscription"
        body = (
            f"Hello {name} team,\n\nPlease cancel my {name} subscription linked to this "
            "email address, effective immediately, and stop any further renewals or "
            "auto-debits. Please confirm by reply once this is done."
        )

    return DraftEmail(
        to=to,
        subject=(DEMO_PREFIX if demo else "") + subject,
        body=body + sign,
        cancel_url=m.cancel.get("url") if m else None,
    )


def create_gmail_draft(service: Any, draft: DraftEmail) -> str:
    raw = base64.urlsafe_b64encode(bytes(draft.to_mime())).decode()
    resp = service.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
    return str(resp["id"])
