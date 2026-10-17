"""Shared detector plumbing: ledger rows -> charges -> recurring series.

A *series* is one merchant on one account charging at a regular cadence.
Most detectors are a few lines on top of series.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Any

from leak.findings import PER_YEAR, EvidenceRef
from leak.ledger.merchants import Catalog, clean_key, default_catalog
from leak.store import Store

# cadence -> (target interval days, tolerance days)
CADENCES: dict[str, tuple[int, int]] = {
    "weekly": (7, 2),
    "monthly": (30, 4),
    "quarterly": (91, 10),
    "yearly": (365, 20),
}
TRIAL_MAX_INR = Decimal(10)
AMOUNT_TOLERANCE = Decimal("0.10")

# Categories that recur but aren't subscriptions you'd "find" (orders, rides, fees).
NOT_SUBSCRIPTIONS = {"fee", "food_orders", "shopping", "transport", "utilities"}
# Narrations that are money moving between people/accounts, not merchants.
_PERSON = re.compile(r"\[UPI_\d+\]|\b(P2P|P2A|SELF|NWD|ATM|SALARY|RENT)\b", re.I)


@dataclass
class Charge:
    id: str
    txn_date: date
    amount: Decimal
    merchant_id: str | None
    merchant: str
    category: str
    account: str | None
    evidence: list[str]
    currency: str
    original_amount: Decimal
    description: str
    related_to: str | None = None

    @property
    def key(self) -> str:
        return self.merchant_id or "?" + clean_key(self.description)

    def refs(self, note: str | None = None) -> list[EvidenceRef]:
        return [EvidenceRef(evidence_id=e, note=note) for e in self.evidence]


@dataclass
class Series:
    key: str
    merchant_id: str | None
    merchant: str
    category: str
    account: str | None
    charges: list[Charge]
    cadence: str
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def last(self) -> Charge:
        return self.charges[-1]

    @property
    def per_year(self) -> int:
        return PER_YEAR[self.cadence]

    @property
    def annual(self) -> Decimal:
        return self.last.amount * self.per_year

    def next_renewal(self) -> date:
        for e in reversed(self.events):  # an email saying "renews on" beats arithmetic
            if e.get("next_date"):
                return date.fromisoformat(e["next_date"])
        return self.last.txn_date + timedelta(days=CADENCES[self.cadence][0])

    def refs(self, last_n: int | None = None) -> list[EvidenceRef]:
        charges = self.charges[-last_n:] if last_n else self.charges
        return [r for c in charges for r in c.refs(f"{c.txn_date} ₹{c.amount}")]


@dataclass
class Context:
    """Everything detectors read. Built once per run."""

    charges: list[Charge]
    events: list[dict[str, Any]]
    catalog: Catalog
    series: list[Series] = field(default_factory=list)

    def events_for(self, merchant_id: str | None, kind: str | None = None) -> list[dict[str, Any]]:
        return [
            e
            for e in self.events
            if e["merchant_id"] == merchant_id
            and merchant_id is not None
            and (kind is None or e["kind"] == kind)
        ]


def load_context(store: Store, catalog: Catalog | None = None) -> Context:
    cat = catalog or default_catalog()
    charges = [
        Charge(
            id=r["id"],
            txn_date=date.fromisoformat(r["txn_date"]),
            amount=Decimal(str(r["amount_inr"])).quantize(Decimal("0.01")),
            merchant_id=r["merchant_id"],
            merchant=r["merchant"],
            category=r["category"],
            account=r["account"],
            evidence=r["evidence"].split(","),
            currency=r["currency"],
            original_amount=Decimal(str(r["original_amount"])),
            description=r["description"],
            related_to=r["related_to"],
        )
        for r in store.ledger()
        if r["direction"] == "debit"
    ]
    ctx = Context(charges=charges, events=store.events(), catalog=cat)
    ctx.series = build_series(ctx)
    return ctx


def is_candidate(c: Charge) -> bool:
    return c.category not in NOT_SUBSCRIPTIONS and not _PERSON.search(c.description)


def detect_cadence(dates: list[date]) -> str | None:
    gaps = [(b - a).days for a, b in pairwise(dates)]
    if not gaps:
        return None
    for name, (target, tol) in CADENCES.items():
        if all(abs(g - target) <= tol for g in gaps):
            return name
    return None


def stable_amounts(amounts: list[Decimal]) -> bool:
    """Stable within ±10%, allowing one step change (a price hike)."""
    steps = sum(abs(b - a) > a * AMOUNT_TOLERANCE for a, b in pairwise(amounts))
    return steps <= 1


def build_series(ctx: Context) -> list[Series]:
    groups: dict[tuple[str, str | None], list[Charge]] = {}
    for c in ctx.charges:
        if is_candidate(c):
            groups.setdefault((c.key, c.account), []).append(c)

    out: list[Series] = []
    for (key, account), charges in groups.items():
        charges.sort(key=lambda c: c.txn_date)
        # A ₹1 trial before the real charges is a different signal (trial
        # detector); keep it out of cadence/price maths.
        while len(charges) > 1 and charges[0].amount <= TRIAL_MAX_INR < charges[1].amount:
            charges = charges[1:]
        first = charges[0]
        merchant = ctx.catalog.get(first.merchant_id) if first.merchant_id else None
        events = ctx.events_for(first.merchant_id)
        cadence = detect_cadence([c.txn_date for c in charges]) if len(charges) > 1 else None
        typical = merchant.typical if merchant else {}

        if cadence and stable_amounts([c.amount for c in charges]):
            enough = len(charges) >= 3 or cadence == "yearly" or cadence in typical
            if not enough:
                continue
        elif len(charges) == 1 and merchant:
            # One charge of a known subscription: recurring if an email states a
            # cadence, or the amount matches the catalog's typical price.
            stated = next(
                (e["cadence"] for e in events if e.get("cadence") and e["cadence"] != "one_off"),
                None,
            )
            by_price = next(
                (
                    cad
                    for cad, price in typical.items()
                    if abs(first.amount - price) <= Decimal(price) * Decimal("0.15")
                ),
                None,
            )
            cadence = stated or by_price
            if not cadence or cadence not in CADENCES:
                continue
        else:
            continue
        out.append(
            Series(
                key=key,
                merchant_id=first.merchant_id,
                merchant=first.merchant,
                category=first.category,
                account=account,
                charges=charges,
                cadence=cadence,
                events=events,
            )
        )
    return out


def median_amount(charges: list[Charge]) -> Decimal:
    return Decimal(str(statistics.median([float(c.amount) for c in charges]))).quantize(
        Decimal("0.01")
    )
