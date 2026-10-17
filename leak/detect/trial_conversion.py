"""Trial -> paid: a ₹0-₹10 charge followed by a full-price charge from the
same merchant within 3-35 days. The classic "free trial, forgot to cancel"."""

from datetime import date

from leak.detect.common import TRIAL_MAX_INR, Context
from leak.findings import EvidenceRef, Finding


def detect(ctx: Context, today: date) -> list[Finding]:
    out = []
    by_key: dict[str, list] = {}  # type: ignore[type-arg]
    for c in sorted(ctx.charges, key=lambda c: c.txn_date):
        by_key.setdefault(c.key, []).append(c)
    for charges in by_key.values():
        for i, trial in enumerate(charges):
            if trial.amount > TRIAL_MAX_INR:
                continue
            full = next(
                (
                    c
                    for c in charges[i + 1 :]
                    if c.amount > TRIAL_MAX_INR and 3 <= (c.txn_date - trial.txn_date).days <= 35
                ),
                None,
            )
            if not full:
                continue
            series = next(
                (s for s in ctx.series if s.key == full.key and s.account == full.account), None
            )
            cadence = series.cadence if series else "monthly"
            per_year = series.per_year if series else 12
            notices = [e for e in ctx.events_for(full.merchant_id, "renewal") if e["is_trial"]]
            out.append(
                Finding(
                    type="trial_conversion",
                    merchant=full.merchant,
                    merchant_id=full.merchant_id,
                    amount_inr=full.amount,
                    cadence=cadence,
                    annual_impact_inr=full.amount * per_year,
                    next_renewal=series.next_renewal() if series else None,
                    confidence=0.9 if notices else 0.75,
                    suggested_action="review",
                    explanation=(
                        f"A ₹{trial.amount:,.0f} trial on {trial.txn_date} turned into a "
                        f"₹{full.amount:,.0f} {cadence} charge on {full.txn_date}."
                    ),
                    evidence=trial.refs("trial charge")
                    + full.refs("first full charge")
                    + [
                        EvidenceRef(evidence_id=e["evidence_id"], note="trial email")
                        for e in notices
                    ],
                    account=full.account,
                ).with_id(str(trial.txn_date))
            )
            break
    return out
