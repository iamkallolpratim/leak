"""Build the normalized ledger from statements + extractions.

Three sources often describe the *same* charge:

    statement  05/04/26 UPI-NETFLIX-netflix@hdfcbank-...      649.00   (canonical)
    SMS        Rs.649.00 debited from a/c [ACCT_3] ... netflix@hdfcbank
    email      Netflix receipt: Rs 649.00, billed 05 April 2026

We merge them into one ledger entry carrying all three evidence ids, so a
finding can cite the bank line *and* the receipt. Matching rule: same merchant
(catalog id, else cleaned narration), same direction, compatible account,
dates within ±3 days, amounts within ₹1/1% (6% when one side was converted
from a foreign currency). Statement lines are processed first and never merge
with each other -- two identical charges on one day are two charges.

Non-charge facts (renewal notices, price changes, cancellations, receipts)
go to `events`, which the Phase 4 detectors read.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from leak.extract.schemas import (
    Cancellation,
    DocExtraction,
    PriceChange,
    Receipt,
    RenewalNotice,
    Transaction,
)
from leak.ingest.models import Document, RawTransaction
from leak.ledger.fx import to_inr
from leak.ledger.merchants import (
    Catalog,
    Merchant,
    clean_key,
    default_catalog,
    resolve_extracted,
)
from leak.store import Store

Source = Literal["statement", "sms", "email"]
_PRIORITY = {"statement": 0, "sms": 1, "email": 2}
DATE_WINDOW_DAYS = 3


@dataclass
class Candidate:
    txn_date: date
    amount_inr: Decimal
    original_amount: Decimal
    currency: str
    direction: str
    merchant_text: str
    merchant: Merchant | None
    account: str | None
    channel: str
    source: Source
    evidence_id: str
    converted: bool = False  # amount_inr came from our FX table, not the bank

    @property
    def key(self) -> str:
        return self.merchant.id if self.merchant else "?" + clean_key(self.merchant_text)


@dataclass
class Entry:
    first: Candidate
    evidence: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    related_to: str | None = None

    @property
    def id(self) -> str:
        return "L" + hashlib.sha256(self.first.evidence_id.encode()).hexdigest()[:10]


class LedgerReport(BaseModel):
    entries: int
    multi_source: int
    events: int
    unresolved_merchants: int
    by_source: dict[str, int]


def _channel(t: RawTransaction) -> str:
    d = t.description.upper()
    if "UPI" in d:
        return "upi"
    if "ACH" in d:
        return "ach"
    if re.search(r"\b(NEFT|IMPS|RTGS|BILLPAY)\b", d):
        return "netbanking"
    return "card" if t.account.startswith("[CARD") else "unknown"


def _statement_candidates(store: Store, cat: Catalog) -> list[Candidate]:
    out = []
    for t in store.raw_transactions():
        foreign = t.foreign_currency and t.foreign_amount is not None
        out.append(
            Candidate(
                txn_date=t.txn_date,
                amount_inr=t.amount,
                original_amount=t.foreign_amount if foreign and t.foreign_amount else t.amount,
                currency=t.foreign_currency if foreign and t.foreign_currency else "INR",
                direction=t.direction,
                merchant_text=t.description,
                merchant=cat.resolve(t.description),
                account=t.account,
                channel=_channel(t),
                source="statement",
                evidence_id=t.evidence_id,
            )
        )
    return out


def _extracted_candidates(
    doc: Document, ex: DocExtraction, cat: Catalog
) -> tuple[list[Candidate], list[dict[str, object]]]:
    cands: list[Candidate] = []
    events: list[dict[str, object]] = []
    src: Source = "sms" if doc.source == "sms" else "email"
    fallback_date = doc.ts.date() if doc.ts else None

    def merchant_for(text: str, plan: str | None = None) -> Merchant | None:
        return resolve_extracted(cat, text, plan, doc.sender, doc.title)

    charges: list[Transaction | Receipt] = [*ex.transactions, *ex.receipts]
    seen: set[tuple[Decimal, date]] = set()
    for r in charges:
        when = r.txn_date or fallback_date
        if when is None or (r.amount, when) in seen:
            continue  # a doc stating the same charge as txn *and* receipt counts once
        seen.add((r.amount, when))
        inr = to_inr(r.amount, r.currency, when)
        cands.append(
            Candidate(
                txn_date=when,
                amount_inr=inr,
                original_amount=r.amount,
                currency=r.currency.upper(),
                direction=r.direction if isinstance(r, Transaction) else "debit",
                merchant_text=r.merchant,
                merchant=merchant_for(r.merchant, getattr(r, "plan", None)),
                account=r.account,
                channel=r.channel if isinstance(r, Transaction) else "unknown",
                source=src,
                evidence_id=doc.id,
                converted=r.currency.upper() != "INR",
            )
        )
    facts: list[Receipt | RenewalNotice | PriceChange | Cancellation] = [
        *ex.receipts, *ex.renewals, *ex.price_changes, *ex.cancellations,
    ]  # fmt: skip
    for rec in facts:
        m = merchant_for(rec.merchant, rec.plan)
        data = rec.model_dump(mode="json")
        when = next((data[k] for k in ("txn_date", "renewal_date", "effective_date")
                     if data.get(k)), None)  # fmt: skip
        events.append({
            "id": "E" + hashlib.sha256(f"{doc.id}|{data}".encode()).hexdigest()[:10],
            "kind": rec.kind,
            "merchant_id": m.id if m else None,
            "merchant": m.name if m else rec.merchant,
            "event_date": when or (fallback_date.isoformat() if fallback_date else None),
            "amount": float(data["amount"]) if data.get("amount") is not None else None,
            "old_amount": float(data["old_amount"]) if data.get("old_amount") else None,
            "new_amount": float(data["new_amount"]) if data.get("new_amount") else None,
            "currency": data.get("currency", "INR"),
            "cadence": data.get("cadence"),
            "is_trial": bool(data.get("is_trial")),
            "plan": data.get("plan"),
            "next_date": data.get("next_billing_date"),
            "evidence_id": doc.id,
        })  # fmt: skip
    return cands, events


def _matches(e: Entry, c: Candidate) -> bool:
    f = e.first
    if f.key != c.key or f.direction != c.direction or c.source in e.sources:
        return False
    if f.account and c.account and f.account != c.account:
        return False
    if abs((f.txn_date - c.txn_date).days) > DATE_WINDOW_DAYS:
        return False
    tol = (
        Decimal("0.06") if (f.converted or c.converted or f.currency != "INR") else Decimal("0.01")
    )
    return abs(f.amount_inr - c.amount_inr) <= max(Decimal(1), f.amount_inr * tol)


def build_ledger(store: Store, catalog: Catalog | None = None) -> LedgerReport:
    cat = catalog or default_catalog()
    cands = _statement_candidates(store, cat)
    events: list[dict[str, object]] = []
    extractions = store.extractions()
    for doc in store.documents():
        if (ex := extractions.get(doc.id)) is not None:
            doc_cands, doc_events = _extracted_candidates(doc, ex, cat)
            cands += doc_cands
            events += doc_events

    entries: list[Entry] = []
    for c in sorted(cands, key=lambda c: (_PRIORITY[c.source], c.txn_date, c.evidence_id)):
        match = None
        if c.source != "statement":
            match = next((e for e in entries if _matches(e, c)), None)
        if match is None:
            match = Entry(first=c)
            entries.append(match)
        match.evidence.append(c.evidence_id)
        match.sources.append(c.source)

    # Link fee lines (forex markup, GST on it) to the foreign charge they belong to.
    for e in entries:
        if e.first.merchant and e.first.merchant.category == "fee":
            e.related_to = next(
                (x.id for x in entries if x.first.currency != "INR"
                 and x.first.txn_date == e.first.txn_date and x.first.account == e.first.account),
                None,
            )  # fmt: skip

    rows = []
    for e in entries:
        f = e.first
        rows.append({
            "id": e.id, "txn_date": f.txn_date.isoformat(),
            "merchant_id": f.merchant.id if f.merchant else None,
            "merchant": f.merchant.name if f.merchant else clean_key(f.merchant_text),
            "category": f.merchant.category if f.merchant else "unknown",
            "description": f.merchant_text, "amount_inr": float(f.amount_inr),
            "currency": f.currency, "original_amount": float(f.original_amount),
            "direction": f.direction, "account": f.account, "channel": f.channel,
            "sources": ",".join(e.sources), "evidence": ",".join(e.evidence),
            "related_to": e.related_to,
        })  # fmt: skip
    store.replace_ledger(rows, events)

    by_source: dict[str, int] = {}
    for e in entries:
        k = "+".join(sorted(set(e.sources), key=_PRIORITY.__getitem__))
        by_source[k] = by_source.get(k, 0) + 1
    return LedgerReport(
        entries=len(entries),
        multi_source=sum(len(set(e.sources)) > 1 for e in entries),
        events=len(events),
        unresolved_merchants=sum(e.first.merchant is None for e in entries),
        by_source=by_source,
    )
