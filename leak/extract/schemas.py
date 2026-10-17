"""What extraction produces from one document.

These schemas double as the JSON schema sent to the model (structured
output), so field descriptions are written for the model as much as for us.
Money is `Amount`: a Decimal internally, a plain JSON number on the wire.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, Field, WithJsonSchema

Amount = Annotated[Decimal, WithJsonSchema({"type": "number"})]
Cadence = Literal["weekly", "monthly", "quarterly", "yearly", "one_off"]
Channel = Literal["card", "upi", "ach", "netbanking", "wallet", "unknown"]

_MERCHANT = "Merchant/service name as written, e.g. 'Netflix' or 'SPOTIFY INDIA'"
_ACCOUNT = "Masked account/card token like [CARD_1] if the text shows one, else null"


class Transaction(BaseModel):
    """Money actually moved (bank/card alert, 'we charged you')."""

    kind: Literal["transaction"] = "transaction"
    txn_date: date | None = None
    amount: Amount
    currency: str = Field("INR", description="ISO code; INR for Rs/₹")
    direction: Literal["debit", "credit"] = "debit"
    merchant: str = Field(description=_MERCHANT)
    account: str | None = Field(None, description=_ACCOUNT)
    channel: Channel = "unknown"


class Receipt(BaseModel):
    """A merchant's receipt/invoice/bill for a payment."""

    kind: Literal["receipt"] = "receipt"
    txn_date: date | None = None
    merchant: str = Field(description=_MERCHANT)
    plan: str | None = Field(None, description="Plan/product, e.g. 'Premium', '100 GB'")
    amount: Amount
    currency: str = "INR"
    cadence: Cadence | None = None
    next_billing_date: date | None = None
    account: str | None = Field(None, description=_ACCOUNT)


class RenewalNotice(BaseModel):
    """An upcoming charge: renewal reminder, trial ending, premium due."""

    kind: Literal["renewal"] = "renewal"
    merchant: str = Field(description=_MERCHANT)
    plan: str | None = None
    renewal_date: date | None = None
    amount: Amount | None = None
    currency: str = "INR"
    cadence: Cadence | None = None
    is_trial: bool = Field(False, description="True if this is a free/cheap trial ending")


class PriceChange(BaseModel):
    kind: Literal["price_change"] = "price_change"
    merchant: str = Field(description=_MERCHANT)
    plan: str | None = None
    old_amount: Amount | None = None
    new_amount: Amount
    currency: str = "INR"
    effective_date: date | None = None


class Cancellation(BaseModel):
    """Confirmation that a subscription/membership was cancelled."""

    kind: Literal["cancellation"] = "cancellation"
    merchant: str = Field(description=_MERCHANT)
    plan: str | None = None
    effective_date: date | None = None
    reference: str | None = None


Record = Transaction | Receipt | RenewalNotice | PriceChange | Cancellation


class DocExtraction(BaseModel):
    """Everything found in one document. Empty lists are normal."""

    evidence_id: str = Field(description="The id attribute of the <doc> this came from")
    is_financial: bool = Field(description="False for newsletters, OTPs, chit-chat")
    transactions: list[Transaction] = Field(default_factory=list)
    receipts: list[Receipt] = Field(default_factory=list)
    renewals: list[RenewalNotice] = Field(default_factory=list)
    price_changes: list[PriceChange] = Field(default_factory=list)
    cancellations: list[Cancellation] = Field(default_factory=list)

    def records(self) -> list[Record]:
        return [*self.transactions, *self.receipts, *self.renewals, *self.price_changes,
                *self.cancellations]  # fmt: skip


class BatchExtraction(BaseModel):
    documents: list[DocExtraction]
