"""The Finding: Leak's unit of output.

Deterministic detectors and the auditor agent both produce Findings. A
finding is only ever shown if every evidence id resolves to a real statement
line, email or SMS (see leak/harness/grounding.py).
"""

from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from leak.extract.schemas import Amount, Cadence

FindingType = Literal[
    "recurring", "price_hike", "trial_conversion", "duplicate", "zombie", "forex", "unused"
]
Action = Literal["cancel", "downgrade", "dispute", "review", "keep"]
SAVING_ACTIONS: frozenset[str] = frozenset({"cancel", "downgrade", "dispute"})
PER_YEAR: dict[str, int] = {"weekly": 52, "monthly": 12, "quarterly": 4, "yearly": 1, "one_off": 0}


class EvidenceRef(BaseModel):
    evidence_id: str = Field(description="e.g. pdf:ab12cd34:p1:l14, gmail:<id>, sms:<id>")
    note: str | None = Field(None, description="What this piece of evidence shows")


class Finding(BaseModel):
    id: str = ""
    type: FindingType
    merchant: str
    merchant_id: str | None = None
    amount_inr: Amount = Field(description="Per-charge amount in INR")
    cadence: Cadence | None = None
    annual_impact_inr: Amount = Field(description="What this costs per year if left alone")
    next_renewal: date | None = None
    confidence: float = Field(ge=0, le=1)
    explanation: str = Field(description="Plain English, 1-3 sentences")
    evidence: list[EvidenceRef] = Field(min_length=1)
    suggested_action: Action
    source: Literal["detector", "agent"] = "detector"
    account: str | None = None

    def with_id(self, *parts: str) -> Finding:
        prefix = "F" if self.source == "detector" else "A"
        key = "|".join([self.type, self.merchant_id or self.merchant, *parts])
        return self.model_copy(
            update={"id": f"{prefix}-{hashlib.sha256(key.encode()).hexdigest()[:8]}"}
        )


def potential_savings(findings: list[Finding]) -> Decimal:
    """Annual ₹ from findings the user can act on (cancel/downgrade/dispute).

    One subscription can trip several detectors (Hotstar: trial conversion
    *and* bundled duplicate). Counting both would double-count, so we take
    the largest actionable impact per merchant."""
    best: dict[str, Decimal] = {}
    for f in findings:
        if f.suggested_action in SAVING_ACTIONS:
            k = f.merchant_id or f.merchant.lower()
            best[k] = max(best.get(k, Decimal(0)), Decimal(f.annual_impact_inr))
    return sum(best.values(), Decimal(0))
