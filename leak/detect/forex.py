"""Forex leakage: foreign-currency charges plus the markup + GST your card adds.

Fee lines are linked to their foreign charge during ledger build
(`related_to`). Impact = average fees per charge x charges per year.
"""

from datetime import date
from decimal import Decimal

from leak.detect.common import Context
from leak.findings import Finding


def detect(ctx: Context, today: date) -> list[Finding]:
    out = []
    by_id = {c.id: c for c in ctx.charges}
    fees_by_merchant: dict[str, list] = {}  # type: ignore[type-arg]
    for f in ctx.charges:
        if f.category == "fee" and f.related_to and (base := by_id.get(f.related_to)):
            fees_by_merchant.setdefault(base.key, []).append((base, f))
    for pairs in fees_by_merchant.values():
        bases = {b.id: b for b, _ in pairs}
        total_fees = sum((f.amount for _, f in pairs), Decimal(0))
        per_charge = (total_fees / len(bases)).quantize(Decimal("0.01"))
        base = next(iter(bases.values()))
        series = next((s for s in ctx.series if s.key == base.key), None)
        per_year = series.per_year if series else len(bases)
        annual = per_charge * per_year
        pct = per_charge / base.amount * 100
        out.append(
            Finding(
                type="forex",
                merchant=base.merchant,
                merchant_id=base.merchant_id,
                amount_inr=per_charge,
                cadence=series.cadence if series else None,
                annual_impact_inr=annual,
                confidence=0.85,
                suggested_action="review",
                account=base.account,
                explanation=(
                    f"Each {base.currency} charge from {base.merchant} adds about "
                    f"₹{per_charge:,.0f} ({pct:.1f}%) in forex markup + GST — "
                    f"₹{annual:,.0f}/year. A zero-markup card or INR billing avoids it."
                ),
                evidence=[r for _, f in pairs[-4:] for r in f.refs("fee line")]
                + base.refs("USD charge"),
            ).with_id(base.account or "")
        )
    return out
