"""Price hikes: latest charge >=5% above the previous one in a series, or a
"your price is changing" email. Impact = the *extra* cost per year."""

from datetime import date
from decimal import Decimal

from leak.detect.common import Context
from leak.findings import PER_YEAR, EvidenceRef, Finding

MIN_HIKE = Decimal("0.05")


def detect(ctx: Context, today: date) -> list[Finding]:
    out: list[Finding] = []
    seen: set[str | None] = set()
    for s in ctx.series:
        for prev, cur in zip(s.charges, s.charges[1:], strict=False):
            if cur.amount > prev.amount * (1 + MIN_HIKE):
                notice = s.events and [e for e in s.events if e["kind"] == "price_change"]
                refs = prev.refs(f"old price ₹{prev.amount}") + cur.refs(f"new price ₹{cur.amount}")
                refs += [
                    EvidenceRef(evidence_id=e["evidence_id"], note="price change email")
                    for e in (notice or [])
                ]
                pct = (cur.amount / prev.amount - 1) * 100
                out.append(
                    Finding(
                        type="price_hike",
                        merchant=s.merchant,
                        merchant_id=s.merchant_id,
                        amount_inr=cur.amount,
                        cadence=s.cadence,
                        annual_impact_inr=(cur.amount - prev.amount) * s.per_year,
                        next_renewal=s.next_renewal(),
                        confidence=0.95 if notice else 0.85,
                        explanation=(
                            f"{s.merchant} went from ₹{prev.amount:,.0f} to "
                            f"₹{cur.amount:,.0f} (+{pct:.0f}%) on {cur.txn_date}; that's "
                            f"₹{(cur.amount - prev.amount) * s.per_year:,.0f}/year more."
                        ),
                        evidence=refs,
                        suggested_action="review",
                        account=s.account,
                    ).with_id(s.account or "", str(cur.txn_date))
                )
                seen.add(s.merchant_id)
    # Announced but not (yet) seen in charges.
    for e in ctx.events:
        if e["kind"] != "price_change" or e["merchant_id"] in seen or not e.get("new_amount"):
            continue
        new, old = Decimal(str(e["new_amount"])), Decimal(str(e.get("old_amount") or 0))
        m = ctx.catalog.get(e["merchant_id"]) if e["merchant_id"] else None
        cadence = next(iter(m.typical), "monthly") if m and m.typical else "monthly"
        out.append(
            Finding(
                type="price_hike",
                merchant=e["merchant"],
                merchant_id=e["merchant_id"],
                amount_inr=new,
                cadence=cadence,
                annual_impact_inr=max(new - old, Decimal(0)) * PER_YEAR[cadence],
                confidence=0.6,
                suggested_action="review",
                explanation=f"{e['merchant']} announced a new price of ₹{new:,.0f}"
                + (f" (was ₹{old:,.0f})." if old else "."),
                evidence=[EvidenceRef(evidence_id=e["evidence_id"], note="price change email")],
            ).with_id("announced")
        )
    return out
