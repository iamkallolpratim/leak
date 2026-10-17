"""Records produced by ingestion.

Every record carries an `evidence_id` -- the citation later findings must use:

    gmail:<message_id>                 an email
    pdf:<file_hash8>:p<page>           a statement page
    pdf:<file_hash8>:p<page>:l<line>   one line on that page (a transaction)
    sms:<hash12>                       one SMS
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

Source = Literal["gmail", "statement", "sms"]


class Document(BaseModel):
    id: str  # evidence id
    source: Source
    title: str = ""  # email subject / "HDFC statement p2" / SMS sender
    sender: str = ""  # redacted
    ts: datetime | None = None
    raw_text: str  # stays local, never sent anywhere
    redacted_text: str  # the only text tools may hand to an LLM
    content_hash: str
    meta: dict[str, Any] = Field(default_factory=dict)


class RawTransaction(BaseModel):
    """One statement line, as parsed. Normalization happens in Phase 3."""

    evidence_id: str
    document_id: str
    bank: str
    parser: str  # e.g. "hdfc_savings" or "llm_fallback"
    account: str  # vault token, e.g. [ACCT_1] / [CARD_2]
    txn_date: date
    description: str  # redacted
    amount: Decimal  # always positive
    direction: Literal["debit", "credit"]
    currency: str = "INR"
    foreign_amount: Decimal | None = None
    foreign_currency: str | None = None
    balance: Decimal | None = None
