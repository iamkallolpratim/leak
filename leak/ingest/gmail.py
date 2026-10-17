"""Gmail ingestion.

Auth is bring-your-own: you create an OAuth "Desktop app" client in your own
Google Cloud project and point `[gmail] client_secret` at its JSON. Leak never
ships a client ID, so no third party ever holds a token to your inbox.

Scopes:
    gmail.readonly   always (search + read)
    gmail.compose    only if `[gmail] enable_actions = true` (create drafts;
                     compose can't send without a separate send call, and Leak
                     has no send tool at all)

Sync is incremental by query: the first run fetches `query` (default: two
years of receipts/renewals/etc.), later runs add `after:<last sync - 1 day>`.
Message IDs dedupe the overlap. The cursor only advances after a complete,
error-free, unlimited run -- a `--limit 100` sample must not make a later
full scan skip everything older than the sample. We fetch `format=raw` and parse RFC 822 with
the stdlib, so demo `.eml` files and live Gmail share one code path.
"""

from __future__ import annotations

import base64
import email
import email.policy
import hashlib
import os
import re
import time
from datetime import UTC, datetime
from email.message import EmailMessage
from email.parser import BytesHeaderParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from leak.ingest.models import Document
from leak.ingest.progress import NullProgress, Progress
from leak.privacy.redact import Redactor
from leak.store import Store

SCOPE_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
SCOPE_COMPOSE = "https://www.googleapis.com/auth/gmail.compose"

DEFAULT_QUERY = (
    "newer_than:2y {subscription receipt invoice renewal renew membership trial "
    'autopay "auto-debit" mandate billing "payment received" "order confirmation" '
    'cancelled cancellation "price change" "price increase" "your plan"}'
)


# --- parsing (pure, testable) ---------------------------------------------------


class _TextExtractor(HTMLParser):
    BLOCK: ClassVar[set[str]] = {
        "p",
        "div",
        "br",
        "tr",
        "li",
        "h1",
        "h2",
        "h3",
        "h4",
        "table",
        "section",
    }

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "head"):
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        elif tag == "td":
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "head"):
            self._skip = max(0, self._skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html)
    text = "".join(p.parts)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


class ParsedEmail(BaseModel):
    subject: str
    sender: str
    to: str
    date: datetime | None
    body: str


def parse_rfc822(raw: bytes) -> ParsedEmail:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    assert isinstance(msg, EmailMessage)
    plain = msg.get_body(preferencelist=("plain",))
    html = msg.get_body(preferencelist=("html",))
    if plain is not None:
        body = str(plain.get_content())
    elif html is not None:
        body = html_to_text(str(html.get_content()))
    else:
        body = ""
    try:
        sent = parsedate_to_datetime(str(msg["Date"])) if msg["Date"] else None
    except (TypeError, ValueError):
        sent = None
    return ParsedEmail(
        subject=str(msg["Subject"] or ""),
        sender=str(msg["From"] or ""),
        to=str(msg["To"] or ""),
        date=sent,
        body=body.strip(),
    )


def email_document(msg_id: str, raw: bytes, redactor: Redactor) -> Document:
    e = parse_rfc822(raw)
    text = f"From: {e.sender}\nTo: {e.to}\nSubject: {e.subject}\n\n{e.body}"
    return Document(
        id=f"gmail:{msg_id}",
        source="gmail",
        title=redactor.redact(e.subject),
        sender=redactor.redact(e.sender),
        ts=e.date,
        raw_text=text,
        redacted_text=redactor.redact(text),
        content_hash=hashlib.sha256(raw).hexdigest(),
    )


def _eml_date(path: Path) -> datetime:
    with path.open("rb") as f:
        date = BytesHeaderParser(policy=email.policy.default).parse(f)["Date"]
    try:
        return parsedate_to_datetime(str(date))
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=UTC)


def ingest_eml_dir(
    directory: Path,
    *,
    store: Store,
    redactor: Redactor,
    limit: int | None = None,
    progress: Progress | None = None,
) -> int:
    """Offline path (demo + tests): each file's stem is its message id.
    `limit` keeps the newest N by Date header, like Gmail's newest-first list."""
    progress = progress or NullProgress()
    paths = sorted(directory.glob("*.eml"))
    if limit is not None:
        paths = sorted(paths, key=_eml_date, reverse=True)[:limit]
    progress.start("eml", "Reading emails", len(paths))
    for path in paths:
        doc = email_document(path.stem, path.read_bytes(), redactor)
        store.upsert_document(doc)
        progress.advance("eml", detail=doc.title)
    progress.finish("eml", f"{len(paths)} emails")
    return len(paths)


# --- OAuth + live sync -------------------------------------------------------------


def get_credentials(client_secret: Path, token_path: Path, *, enable_actions: bool) -> Any:
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    scopes = [SCOPE_READONLY] + ([SCOPE_COMPOSE] if enable_actions else [])
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path))  # type: ignore[no-untyped-call]
        if not set(scopes) <= set(creds.scopes or []):
            creds = None  # scopes changed (e.g. actions just enabled): re-consent
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            # Google revokes refresh tokens of "Testing"-mode OAuth apps after 7
            # days; just log in again instead of crashing.
            creds = None
    if not creds or not creds.valid:
        if not client_secret.exists():
            raise FileNotFoundError(
                f"OAuth client file not found: {client_secret}. Create a Desktop OAuth "
                "client in your Google Cloud project and download its JSON there."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), scopes)
        creds = flow.run_local_server(port=0)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    os.chmod(token_path, 0o600)
    return creds


def build_service(creds: Any) -> Any:
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


class GmailSyncStats(BaseModel):
    listed: int = 0
    fetched: int = 0
    skipped_known: int = 0
    errors: int = 0
    first_error: str | None = None
    limited: bool = False
    cursor_updated: bool = False


class GmailSync:
    CURSOR = "gmail"

    def __init__(
        self,
        service: Any,
        *,
        store: Store,
        redactor: Redactor,
        query: str = DEFAULT_QUERY,
        max_messages: int = 2000,
        limit: int | None = None,
        progress: Progress | None = None,
    ) -> None:
        self.service = service
        self.store = store
        self.redactor = redactor
        self.query = query
        # `limit` is a user-requested sample (newest N); `max_messages` is a safety cap.
        self.limit = limit
        self.max_messages = min(max_messages, limit) if limit else max_messages
        self.progress = progress or NullProgress()

    def _query(self) -> str:
        cursor = self.store.get_cursor(self.CURSOR)
        if cursor:
            return f"{self.query} after:{int(cursor) - 86_400}"
        return self.query

    def _list_ids(self, q: str) -> list[str]:
        ids: list[str] = []
        token: str | None = None
        self.progress.start("gmail-list", "Listing Gmail", None)
        while len(ids) < self.max_messages:
            # Gmail lists newest first, so capping the page size gives "last N".
            page_size = min(500, self.max_messages - len(ids))
            resp = (
                self.service.users()
                .messages()
                .list(userId="me", q=q, maxResults=page_size, pageToken=token)
                .execute()
            )
            page = [m["id"] for m in resp.get("messages", [])]
            ids += page
            self.progress.advance("gmail-list", len(page), detail=f"{len(ids)} found")
            token = resp.get("nextPageToken")
            if not token:
                break
        ids = ids[: self.max_messages]
        self.progress.finish("gmail-list", f"{len(ids)} matching messages")
        return ids

    def sync(self) -> GmailSyncStats:
        started = int(time.time())
        stats = GmailSyncStats(limited=self.limit is not None)
        ids = self._list_ids(self._query())
        stats.listed = len(ids)
        new = [i for i in ids if not self.store.has_document(f"gmail:{i}")]
        stats.skipped_known = len(ids) - len(new)

        self.progress.start("gmail", "Fetching emails", len(new))
        for msg_id in new:
            try:
                msg = (
                    self.service.users()
                    .messages()
                    .get(userId="me", id=msg_id, format="raw")
                    .execute()
                )
                doc = email_document(msg_id, base64.urlsafe_b64decode(msg["raw"]), self.redactor)
                self.store.upsert_document(doc)
                stats.fetched += 1
                self.progress.advance("gmail", detail=doc.title)
            except Exception as e:  # one bad message must not sink the sync
                stats.errors += 1
                stats.first_error = stats.first_error or f"{msg_id}: {type(e).__name__}: {e}"
                self.progress.advance("gmail", detail=f"error on {msg_id}")
        self.progress.finish("gmail", f"{stats.fetched} new, {stats.skipped_known} known")

        # Failed messages get retried next run (known ones are skipped cheaply).
        if not stats.limited and stats.errors == 0:
            self.store.set_cursor(self.CURSOR, str(started))
            stats.cursor_updated = True
        return stats
