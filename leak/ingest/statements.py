"""Bank / credit-card statement ingestion.

    PDF ──unlock (pikepdf)──> text per page (pdfplumber)
        ├─ a bank plugin recognises it → deterministic regex parse   (free, exact)
        └─ nobody does → LLM fallback on *redacted* numbered lines   (Flash, cheap)

The fallback never uploads the PDF itself: we can't redact inside a PDF, so
the model gets redacted text only. Scanned PDFs with no text layer are
refused rather than uploaded raw (OCR is on the roadmap).
"""

from __future__ import annotations

import hashlib
import io
import re
from collections.abc import Iterable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pdfplumber
import pikepdf
from pydantic import BaseModel, Field

from leak.harness.budget import Budget
from leak.ingest.banks import ParsedLine, ParsedStatement, detect_parser
from leak.ingest.models import Document, RawTransaction
from leak.llm.provider import LLMProvider, Message
from leak.privacy.guard import EgressGuard, GuardedProvider
from leak.privacy.redact import TOKEN_RE, Redactor
from leak.store import Store


class StatementError(Exception):
    pass


class StatementLocked(StatementError):
    pass


class UnsupportedStatement(StatementError):
    pass


# --- unlock + text ---------------------------------------------------------------


def unlock_pdf(path: Path, passwords: Iterable[str] = ()) -> bytes:
    """Return decrypted PDF bytes, in memory. Nothing decrypted touches disk."""
    for pw in ["", *passwords]:
        try:
            with pikepdf.open(path, password=pw) as pdf:
                buf = io.BytesIO()
                pdf.save(buf)
                return buf.getvalue()
        except pikepdf.PasswordError:
            continue
    raise StatementLocked(f"{path.name}: password-protected and no configured password worked")


def pdf_pages_text(data: bytes) -> list[str]:
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return [page.extract_text() or "" for page in pdf.pages]


# --- LLM fallback ----------------------------------------------------------------


class FallbackTxn(BaseModel):
    page: int = Field(description="page number as given in the input")
    line: int = Field(description="line number as given in the input")
    txn_date: date = Field(description="ISO date YYYY-MM-DD")
    description: str
    amount: Decimal = Field(description="positive number, no currency symbol")
    direction: Literal["debit", "credit"]
    foreign_currency: str | None = None
    foreign_amount: Decimal | None = None


class FallbackStatement(BaseModel):
    bank_name: str | None = None
    is_credit_card: bool = False
    account_token: str | None = Field(None, description="e.g. [ACCT_1] or [CARD_2] if shown")
    transactions: list[FallbackTxn]


FALLBACK_SYSTEM = """You extract transactions from bank or credit-card statements.
Input lines are numbered "p<page> l<line>: text". Personal data is already masked
as tokens like [ACCT_1]; keep tokens as-is. Return one entry per transaction line,
citing its exact page and line. Ignore headers, totals, and balance-only lines.
Dates as ISO (YYYY-MM-DD). Amounts as plain positive numbers."""


class LLMStatementParser:
    """Fallback for banks without a plugin. Always runs behind the egress
    guard, and drops any row whose cited line doesn't contain its amount."""

    name = "llm_fallback"

    def __init__(
        self, provider: LLMProvider, model: str, redactor: Redactor, budget: Budget | None = None
    ) -> None:
        self.provider = GuardedProvider(provider, EgressGuard(redactor))
        self.model = model
        self.redactor = redactor
        self.budget = budget

    def parse(self, pages: list[str]) -> ParsedStatement:
        redacted = [self.redactor.redact(p).splitlines() for p in pages]
        numbered = "\n".join(
            f"p{p} l{n}: {text}"
            for p, lines in enumerate(redacted, start=1)
            for n, text in enumerate(lines, start=1)
            if text.strip()
        )
        resp = self.provider.generate(
            model=self.model,
            system=FALLBACK_SYSTEM,
            messages=[Message(role="user", text=numbered)],
            response_schema=FallbackStatement,
        )
        if self.budget:
            self.budget.charge(resp.usage, self.model)
        result = FallbackStatement.model_validate_json(resp.text)

        lines: list[ParsedLine] = []
        for t in result.transactions:
            # Grounding: the cited line must exist and contain the amount.
            if not (1 <= t.page <= len(redacted) and 1 <= t.line <= len(redacted[t.page - 1])):
                continue
            src = redacted[t.page - 1][t.line - 1].replace(",", "")
            if f"{t.amount:.2f}" not in src and str(t.amount) not in src:
                continue
            lines.append(
                ParsedLine(
                    page=t.page,
                    line=t.line,
                    txn_date=t.txn_date,
                    description=t.description,
                    amount=t.amount,
                    direction=t.direction,
                    foreign_amount=t.foreign_amount,
                    foreign_currency=t.foreign_currency,
                )
            )
        hint = result.account_token if result.account_token else None
        return ParsedStatement(
            bank=result.bank_name or "Unknown bank",
            parser=self.name,
            is_credit_card=result.is_credit_card,
            account_hint=hint,
            lines=lines,
        )


# --- pipeline ----------------------------------------------------------------------


class StatementResult(BaseModel):
    file: str
    bank: str
    parser: str
    pages: int
    transactions: int


def ingest_statement(
    path: Path,
    *,
    store: Store,
    redactor: Redactor,
    passwords: Iterable[str] = (),
    fallback: LLMStatementParser | None = None,
) -> StatementResult:
    data = unlock_pdf(path, passwords)
    pages = pdf_pages_text(data)
    # Hash the *text*, not the file: a re-downloaded (re-encrypted) copy of the
    # same statement gets the same evidence ids, so nothing is double-counted.
    file_hash = hashlib.sha256("\f".join(pages).encode()).hexdigest()[:8]
    if not any(p.strip() for p in pages):
        raise UnsupportedStatement(f"{path.name}: no text layer (scanned?); not uploading raw PDF")

    parser_cls = detect_parser(pages[0])
    if parser_cls is not None:
        parsed = parser_cls().parse(pages)
    elif fallback is not None:
        parsed = fallback.parse(pages)
    else:
        raise UnsupportedStatement(f"{path.name}: no bank parser matched and LLM fallback is off")

    account = _account_token(parsed, redactor)
    for i, page in enumerate(pages, start=1):
        store.upsert_document(
            Document(
                id=f"pdf:{file_hash}:p{i}",
                source="statement",
                title=f"{parsed.bank} statement {path.name} p{i}",
                raw_text=page,
                redacted_text=redactor.redact(page),
                content_hash=hashlib.sha256(page.encode()).hexdigest(),
                meta={"file": path.name, "page": i, "parser": parsed.parser, "bank": parsed.bank},
            )
        )
    n = store.upsert_transactions(
        RawTransaction(
            evidence_id=f"pdf:{file_hash}:p{ln.page}:l{ln.line}",
            document_id=f"pdf:{file_hash}:p{ln.page}",
            bank=parsed.bank,
            parser=parsed.parser,
            account=account,
            txn_date=ln.txn_date,
            description=redactor.redact(ln.description),
            amount=ln.amount,
            direction=ln.direction,
            foreign_amount=ln.foreign_amount,
            foreign_currency=ln.foreign_currency,
            balance=ln.balance,
        )
        for ln in parsed.lines
    )
    return StatementResult(
        file=path.name, bank=parsed.bank, parser=parsed.parser, pages=len(pages), transactions=n
    )


def _account_token(parsed: ParsedStatement, redactor: Redactor) -> str:
    hint = parsed.account_hint
    if not hint:
        return "[ACCT_UNKNOWN]"
    if TOKEN_RE.fullmatch(hint.strip()):
        return hint.strip()  # fallback already saw a token
    kind = "CARD" if parsed.is_credit_card else "ACCT"
    return redactor.vault.token_for(kind, re.sub(r"\s+", "", hint))
