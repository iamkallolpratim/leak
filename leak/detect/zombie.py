"""Zombies: you cancelled, the charges didn't stop.

A cancellation-confirmation email exists, and the same merchant charged you
again more than 3 days after the cancellation took effect.
"""

from datetime import date, timedelta

from leak.detect.common import Context
from leak.findings import PER_YEAR, EvidenceRef, Finding

GRACE = timedelta(days=3)


def detect(ctx: Context, today: date) -> list[Finding]:
    out = []
    for e in ctx.events:
        if e["kind"] != "cancellation" or not e["merchant_id"] or not e["event_date"]:
            continue
        cancelled = date.fromisoformat(e["event_date"])
        after = sorted(
            (
                c
                for c in ctx.charges
                if c.merchant_id == e["merchant_id"] and c.txn_date > cancelled + GRACE
            ),
            key=lambda c: c.txn_date,
        )
        if not after:
            continue
        series = next((s for s in ctx.series if s.merchant_id == e["merchant_id"]), None)
        cadence = series.cadence if series else "monthly"
        paid = sum((c.amount for c in after), start=after[0].amount * 0)
        out.append(
            Finding(
                type="zombie",
                merchant=after[0].merchant,
                merchant_id=e["merchant_id"],
                amount_inr=after[-1].amount,
                cadence=cadence,
                annual_impact_inr=after[-1].amount * PER_YEAR[cadence],
                confidence=0.9,
                suggested_action="dispute",
                account=after[-1].account,
                explanation=(
                    f"You cancelled {after[0].merchant} on {cancelled}, but were charged "
                    f"{len(after)} more time{'s' if len(after) > 1 else ''} "
                    f"(₹{paid:,.0f} so far). Ask for a refund and check the mandate is revoked."
                ),
                evidence=[
                    EvidenceRef(evidence_id=e["evidence_id"], note="cancellation confirmation")
                ]
                + [r for c in after for r in c.refs(f"charged after cancelling: ₹{c.amount}")],
            ).with_id(str(cancelled))
        )
    return out
