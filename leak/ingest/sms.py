"""SMS ingestion from Android "SMS Backup & Restore" XML exports.

    <smses count="2">
      <sms address="AX-HDFCBK" date="1712300000000" type="1" body="Rs 649.00 debited..." />
    </smses>

We keep only messages that look like financial alerts from businesses:
Indian commercial senders use DLT headers like "AX-HDFCBK" / "VM-SBICRD-S",
never a 10-digit number. Personal chats and OTPs are dropped before storage.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path

from defusedxml import ElementTree
from pydantic import BaseModel

from leak.ingest.models import Document
from leak.ingest.progress import NullProgress, Progress
from leak.privacy.redact import Redactor
from leak.store import Store

DLT_SENDER = re.compile(r"^[A-Z]{2}-[A-Z0-9]{3,9}(?:-[A-Z])?$")
FINANCIAL = re.compile(
    r"(?i)\b(debit(ed)?|credit(ed)?|spent|charged|autopay|auto-?debit|mandate|renew(al|ed)?|"
    r"subscription|payment|txn|transaction|purchase|bill)\b"
)
OTP = re.compile(r"(?i)\b(otp|one[- ]time password|verification code)\b")


class SmsStats(BaseModel):
    file: str
    kept: int = 0
    skipped_personal: int = 0
    skipped_otp: int = 0
    skipped_other: int = 0


def ingest_sms_backup(
    path: Path, *, store: Store, redactor: Redactor, progress: Progress | None = None
) -> SmsStats:
    progress = progress or NullProgress()
    stats = SmsStats(file=path.name)
    root = ElementTree.parse(path).getroot()
    if root is None:
        return stats
    messages = list(root.iter("sms"))
    progress.start(f"sms:{path.name}", f"SMS {path.name}", len(messages))
    for el in messages:
        progress.advance(f"sms:{path.name}")
        if el.get("type", "1") != "1":  # 1 = inbox; skip sent/drafts
            stats.skipped_other += 1
            continue
        sender = (el.get("address") or "").strip().upper()
        body = el.get("body") or ""
        if not DLT_SENDER.match(sender):
            stats.skipped_personal += 1
            continue
        if OTP.search(body):
            stats.skipped_otp += 1
            continue
        if not FINANCIAL.search(body):
            stats.skipped_other += 1
            continue
        ms = int(el.get("date") or 0)
        digest = hashlib.sha256(f"{sender}|{ms}|{body}".encode()).hexdigest()
        store.upsert_document(
            Document(
                id=f"sms:{digest[:12]}",
                source="sms",
                title=sender,
                sender=sender,  # DLT header identifies a business, not a person
                ts=datetime.fromtimestamp(ms / 1000, tz=UTC),
                raw_text=body,
                redacted_text=redactor.redact(body),
                content_hash=digest,
                meta={"file": path.name},
            )
        )
        stats.kept += 1
    progress.finish(f"sms:{path.name}", f"{stats.kept} kept")
    return stats
