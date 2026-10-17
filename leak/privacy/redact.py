"""PII redaction with a reversible, local-only vault.

Every piece of text that could reach an LLM goes through `Redactor.redact()`
first. Detected PII is replaced by a stable token such as `[CARD_2]`; the
token -> original mapping lives only in a local SQLite vault so the UI can
unmask it. The same value always maps to the same token, so the model can
still reason ("these two charges hit the same card") without seeing it.

Detected (India-first, works elsewhere):
    PAN, Aadhaar (Verhoeff-checked), card numbers (Luhn-checked, full or
    masked like XXXX1234), bank account numbers, phone numbers, email
    addresses, UPI IDs, long reference numbers, plus the user's own
    names / emails / address fragments from config, and labelled addresses.

Bias: when unsure, mask. A masked merchant name costs a little accuracy; a
leaked account number can't be taken back.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import sqlite_utils

# Order = priority when two detections overlap (earlier wins).
KINDS = ("CARD", "AADHAAR", "PAN", "EMAIL", "UPI", "PHONE", "ACCT", "NAME", "ADDRESS", "NUM")
_PRIORITY = {k: i for i, k in enumerate(KINDS)}

TOKEN_RE = re.compile(r"\[(?:" + "|".join(KINDS) + r")_\d+\]")

# Cards: 13-19 contiguous digits, or 4-4-4-4 groups. Must start 2-6 (card IINs).
_CARD_FULL = re.compile(
    r"(?<![\d\w])(?:[2-6]\d{3}[ -]\d{4}[ -]\d{4}[ -]\d{4}(?:[ -]?\d{3})?|[2-6]\d{12,18})(?![\d\w])"
)
_MASKED = re.compile(r"(?<![\w])(?:\d{4,6}[ -]?)?(?:[Xx*•]{2,}[ -]?)+\d{3,4}(?!\d)")
# Aadhaar: printed as "XXXX XXXX XXXX", or 12 digits right after an Aadhaar label.
# Bare 12-digit runs are usually UPI refs; those still get masked, as NUM.
_AADHAAR = re.compile(r"(?<![\d\w])[2-9]\d{3}[ -]\d{4}[ -]\d{4}(?![\d\w])")
_AADHAAR_CTX = re.compile(
    r"(?i)\b(?:aadhaa?r|uid)\s*(?:no\.?|number)?\s*[:#-]?\s*([2-9]\d{11})(?!\d)"
)
_PAN = re.compile(r"\b[A-Z]{3}[ABCFGHLJPT][A-Z]\d{4}[A-Z]\b")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_UPI = re.compile(r"(?<![\w.])[A-Za-z0-9_.]{2,}@[a-zA-Z]{2,}\b(?!\.)")
_PHONE = re.compile(r"(?<![\d+\w])(?:\+?91[ -]?|0)?[6-9]\d{4}[ -]?\d{5}(?!\d)")
_ACCT_CTX = re.compile(
    r"(?i)\b(?:a/?c|acct|account)(?:\s*(?:no|number|num)\b)?\.?\s*[:#-]?\s*(\d{9,18})(?!\d)"
)
# "card ending 4444" / "Visa ending in 1007" / "a/c ending 6789"
_ENDING = re.compile(
    r"(?i)\b(card|visa|mastercard|rupay|amex|a/?c|account)\b[^\n\d]{0,20}?"
    r"\bending\s+(?:in\s+|with\s+)?(\d{4})\b"
)
_NUM = re.compile(r"(?<!\d)\d{9,18}(?!\d)")
_PIN = re.compile(r"(?i)\bpin\s*(?:code)?\s*[:\-]?\s*[1-9]\d{5}\b")
# A whole line that ends "<City>, <State> 560001" is an address line.
_PIN_LINE = re.compile(r"(?m)^[^\n]*[A-Za-z],?[ \t]+[A-Za-z][A-Za-z \t]*[ \t-][1-9]\d{5}[ \t]*$")
_ADDR_LABEL = re.compile(r"(?i)\b(?:[a-z]+[ \t]+)?address[ \t]*[:\-][ \t]*")
_ACCT_WORDS = re.compile(r"(?i)\b(?:a/?c|acct|account)\b")

# Service addresses we keep, so the model can tell Netflix from Spotify.
_ROLE_LOCALPARTS = re.compile(
    r"(?:no-?reply|do-?not-?reply|support|bill|help|info|alert|payment|receipt|care|"
    r"hello|team|order|notif|news|account|member|service|subscri|statement|card|confirm|"
    r"renew|invoice|update|ship|digital|feedback|security|offers?$)",
    re.I,
)
# UPI handles: personal ones (Google Pay "ok*" PSPs, phone-number VPAs) are
# masked; merchant VPAs like netflix@hdfcbank are kept for merchant resolution.
_PERSONAL_VPA = re.compile(r"(?i)^(?:\d{6,}.*|.+@ok[a-z]+)$")
_PERSONAL_DOMAINS = re.compile(
    r"(?i)@(gmail|googlemail|yahoo|ymail|outlook|hotmail|live|icloud|me|rediffmail|proton(mail)?)\."
)


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    kind: str
    value: str


@dataclass
class PrivacyProfile:
    """The user's own identifiers, always masked wherever they appear."""

    names: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    address_fragments: list[str] = field(default_factory=list)


# --- checksums ---------------------------------------------------------------


def luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


_VD = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
    [2, 3, 4, 0, 1, 7, 8, 9, 5, 6], [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
    [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
    [8, 7, 6, 5, 9, 3, 2, 1, 0, 4], [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]  # fmt: skip
_VP = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
    [5, 8, 0, 3, 7, 9, 6, 1, 4, 2], [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
    [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]  # fmt: skip


def verhoeff_ok(digits: str) -> bool:
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _VD[c][_VP[i % 8][int(ch)]]
    return c == 0


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


# --- detection -----------------------------------------------------------------


def _keep_email(addr: str, profile: PrivacyProfile) -> bool:
    if addr.lower() in {e.lower() for e in profile.emails}:
        return False
    local = addr.split("@", 1)[0]
    return bool(_ROLE_LOCALPARTS.search(local)) and not _PERSONAL_DOMAINS.search(addr)


def _keep_vpa(vpa: str, profile: PrivacyProfile) -> bool:
    local = vpa.split("@", 1)[0].lower()
    mine = any(p.lower() in local for p in _name_variants(profile.names))
    return not mine and not _PERSONAL_VPA.match(vpa)


def find_pii(text: str, profile: PrivacyProfile | None = None) -> list[Span]:
    """All PII spans in `text`, non-overlapping, in order."""
    profile = profile or PrivacyProfile()
    cands: list[Span] = []

    def add(m: re.Match[str], kind: str, group: int = 0) -> None:
        cands.append(Span(m.start(group), m.end(group), kind, m.group(group)))

    for m in _CARD_FULL.finditer(text):
        d = _digits(m.group())
        after_acct_label = _ACCT_WORDS.search(text[max(0, m.start() - 20) : m.start()])
        if 13 <= len(d) <= 19 and luhn_ok(d) and not after_acct_label:
            add(m, "CARD")
    for m in _MASKED.finditer(text):
        before = text[max(0, m.start() - 25) : m.start()].lower()
        add(m, "CARD" if "card" in before or len(_digits(m.group())) > 4 else "ACCT")
    for m in _AADHAAR.finditer(text):
        if verhoeff_ok(_digits(m.group())):
            add(m, "AADHAAR")
    for m in _AADHAAR_CTX.finditer(text):
        add(m, "AADHAAR", 1)  # labelled: mask even if the checksum fails
    for m in _PAN.finditer(text):
        add(m, "PAN")
    for m in _EMAIL.finditer(text):
        if not _keep_email(m.group(), profile):
            add(m, "EMAIL")
    for m in _UPI.finditer(text):
        if not _keep_vpa(m.group(), profile):
            add(m, "UPI")
    for m in _PHONE.finditer(text):
        add(m, "PHONE")
    for m in _ACCT_CTX.finditer(text):
        add(m, "ACCT", 1)
    for m in _ENDING.finditer(text):
        kind = "ACCT" if m.group(1).lower().startswith(("a/", "ac")) else "CARD"
        cands.append(Span(m.start(2), m.end(2), kind, "XX" + m.group(2)))
    for m in _NUM.finditer(text):
        add(m, "NUM")
    for m in _PIN.finditer(text):
        add(m, "ADDRESS")
    for m in _PIN_LINE.finditer(text):
        add(m, "ADDRESS")

    for name in _name_variants(profile.names):
        for m in re.finditer(r"(?i)(?<!\w)" + re.escape(name) + r"(?!\w)", text):
            add(m, "NAME")
    for frag in profile.address_fragments:
        # Mask the whole line: "Flat 12B, <fragment>" is all address.
        for m in re.finditer(r"(?im)^[^\n]*" + re.escape(frag) + r"[^\n]*$", text):
            add(m, "ADDRESS")
    cands.extend(_labelled_addresses(text))

    # Already-redacted tokens are not PII (keeps redact() idempotent).
    cands = [s for s in cands if re.search(r"\w", TOKEN_RE.sub("", s.value))]

    # Resolve overlaps: highest priority first, then longest.
    chosen: list[Span] = []
    for s in sorted(cands, key=lambda s: (_PRIORITY[s.kind], -(s.end - s.start))):
        if all(s.end <= c.start or s.start >= c.end for c in chosen):
            chosen.append(s)
    return sorted(chosen, key=lambda s: s.start)


def _name_variants(names: Iterable[str]) -> list[str]:
    out: set[str] = set()
    for n in names:
        out.add(n)
        out.update(p for p in n.split() if len(p) >= 3)
    return sorted(out, key=len, reverse=True)


def _labelled_addresses(text: str) -> list[Span]:
    """'Shipping Address: <rest of line>' or a label followed by up to 3 lines."""
    spans: list[Span] = []
    for m in _ADDR_LABEL.finditer(text):
        line_end = text.find("\n", m.end())
        line_end = len(text) if line_end == -1 else line_end
        if text[m.end() : line_end].strip():
            spans.append(Span(m.end(), line_end, "ADDRESS", text[m.end() : line_end]))
            continue
        # Label alone on its line: take following non-blank lines (max 3).
        start = end = line_end + 1
        for _ in range(3):
            nxt = text.find("\n", end)
            nxt = len(text) if nxt == -1 else nxt
            if not text[end:nxt].strip():
                break
            end = nxt + 1
        if end > start:
            spans.append(Span(start, end - 1, "ADDRESS", text[start : end - 1]))
    return spans


# --- vault -------------------------------------------------------------------------


def _normalize(kind: str, value: str) -> str:
    """Two spellings of the same thing must map to the same token."""
    if kind in ("CARD", "ACCT"):
        return "last4:" + _digits(value)[-4:]  # banks show only last 4 anyway
    if kind == "PHONE":
        return _digits(value)[-10:]
    if kind in ("AADHAAR", "NUM"):
        return _digits(value)
    return re.sub(r"\s+", " ", value.strip().lower())


class Vault:
    """token <-> original value, stored locally. Never sent anywhere.

    Lookups use an HMAC of the normalized value (salted per vault) so the
    index itself doesn't reveal values to someone grepping the file.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite_utils.Database(sqlite3.connect(self.path, timeout=15))
        if "meta" not in self.db.table_names():
            self.db.table("meta").insert({"key": "salt", "value": secrets.token_hex(16)}, pk="key")
            self.db.table("entries").create(
                {"token": str, "kind": str, "digest": str, "value": str}, pk="token"
            )
            self.db.table("entries").create_index(["digest"], unique=True)
            os.chmod(self.path, 0o600)
        self._salt = bytes.fromhex(str(self.db.table("meta").get("salt")["value"]))

    def _digest(self, kind: str, value: str) -> str:
        msg = f"{kind}:{_normalize(kind, value)}".encode()
        return hmac.new(self._salt, msg, hashlib.sha256).hexdigest()

    def token_for(self, kind: str, value: str) -> str:
        digest = self._digest(kind, value)
        rows = list(self.db.query("select token from entries where digest = ?", [digest]))
        if rows:
            return str(rows[0]["token"])
        n = self.db.execute("select count(*) from entries where kind = ?", [kind]).fetchone()[0]
        token = f"[{kind}_{n + 1}]"
        self.db.table("entries").insert(
            {"token": token, "kind": kind, "digest": digest, "value": value}
        )
        return token

    def reveal(self, token: str) -> str | None:
        rows = list(self.db.query("select value from entries where token = ?", [token]))
        return str(rows[0]["value"]) if rows else None

    def unmask(self, text: str) -> str:
        return TOKEN_RE.sub(lambda m: self.reveal(m.group()) or m.group(), text)

    def counts(self) -> dict[str, int]:
        q = "select kind, count(*) as n from entries group by kind"
        return {str(r["kind"]): int(r["n"]) for r in self.db.query(q)}


class Redactor:
    def __init__(self, vault: Vault, profile: PrivacyProfile | None = None) -> None:
        self.vault = vault
        self.profile = profile or PrivacyProfile()

    def redact(self, text: str) -> str:
        out: list[str] = []
        pos = 0
        for s in find_pii(text, self.profile):
            out.append(text[pos : s.start])
            out.append(self.vault.token_for(s.kind, s.value))
            # Keep line count stable so "page 2, line 14" citations still
            # point at the same line in redacted text.
            out.append("\n" * s.value.count("\n"))
            pos = s.end
        out.append(text[pos:])
        return "".join(out)

    def leaks(self, text: str) -> list[Span]:
        """PII still present in `text` (should be empty after redact)."""
        return find_pii(text, self.profile)
