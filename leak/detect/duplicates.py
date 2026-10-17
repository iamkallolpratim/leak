"""Duplicates: paying twice for the same thing.

1. Same service billed on two accounts/cards at the same time.
2. A standalone subscription that's already included in a bundle you pay
   for (Airtel Black includes Hotstar + Prime, per merchants.yaml `includes`).
"""

from datetime import date, timedelta

from leak.detect.common import CADENCES, Context, Series
from leak.findings import EvidenceRef, Finding


def _active(s: Series, today: date) -> bool:
    return today - s.last.txn_date <= timedelta(days=CADENCES[s.cadence][0] * 2 + 7)


def detect(ctx: Context, today: date) -> list[Finding]:
    out: list[Finding] = []
    by_merchant: dict[str, list[Series]] = {}
    for s in ctx.series:
        if s.merchant_id and _active(s, today):
            by_merchant.setdefault(s.merchant_id, []).append(s)

    for mid, group in by_merchant.items():
        accounts = {s.account for s in group}
        if len(group) > 1 and len(accounts) > 1:
            cheaper = min(group, key=lambda s: s.annual)
            out.append(
                Finding(
                    type="duplicate",
                    merchant=cheaper.merchant,
                    merchant_id=mid,
                    amount_inr=cheaper.last.amount,
                    cadence=cheaper.cadence,
                    annual_impact_inr=cheaper.annual,
                    next_renewal=cheaper.next_renewal(),
                    confidence=0.8,
                    suggested_action="cancel",
                    account=cheaper.account,
                    explanation=(
                        f"{cheaper.merchant} is billed on {len(accounts)} accounts "
                        f"({', '.join(sorted(a or '?' for a in accounts))}). If it's the same "
                        "plan twice, cancelling one saves "
                        f"₹{cheaper.annual:,.0f}/year."
                    ),
                    evidence=[r for s in group for r in s.refs(last_n=2)],
                ).with_id("multi-account")
            )

    # Bundles: is something you pay for separately already in a plan you have?
    for bundle in [m for m in ctx.catalog.by_id.values() if m.includes]:
        proof = [
            c
            for c in ctx.charges
            if c.merchant_id == bundle.id and today - c.txn_date <= timedelta(days=120)
        ]
        proof_events = [e for e in ctx.events if e["merchant_id"] == bundle.id]
        if not proof and not proof_events:
            continue
        bundle_refs = [r for c in proof[-1:] for r in c.refs(f"{bundle.name} bill")] or [
            EvidenceRef(evidence_id=proof_events[-1]["evidence_id"], note=f"{bundle.name} bill")
        ]
        for mid in bundle.includes:
            for s in by_merchant.get(mid, []):
                out.append(
                    Finding(
                        type="duplicate",
                        merchant=s.merchant,
                        merchant_id=mid,
                        amount_inr=s.last.amount,
                        cadence=s.cadence,
                        annual_impact_inr=s.annual,
                        next_renewal=s.next_renewal(),
                        confidence=0.7,
                        suggested_action="cancel",
                        account=s.account,
                        explanation=(
                            f"You pay for {s.merchant} separately (₹{s.annual:,.0f}/year), "
                            f"but your {bundle.name} plan appears to include it. Check the "
                            "bundle covers the same tier before cancelling."
                        ),
                        evidence=s.refs(last_n=2) + bundle_refs,
                    ).with_id(bundle.id, s.account or "")
                )
    return out
