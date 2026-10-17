"""Recurring charges: every active series becomes a 'recurring' finding.

Rule: same merchant + account, >=3 charges (>=2 for yearly or a known
cadence for that merchant), regular interval (7/30/91/365 days ± tolerance),
amount stable within ±10% allowing one price step. Single charges of known
subscriptions count when an email states the cadence or the price matches.
"""

from datetime import date, timedelta

from leak.detect.common import Context
from leak.findings import Finding


def detect(ctx: Context, today: date) -> list[Finding]:
    out = []
    for s in ctx.series:
        # Ended? No charge for 2 full periods (and it isn't yearly) -> not active.
        from leak.detect.common import CADENCES

        lapsed = today - s.last.txn_date > timedelta(days=2 * CADENCES[s.cadence][0] + 7)
        n = len(s.charges)
        conf = 0.95 if n >= 3 else (0.8 if n == 2 else 0.65)
        out.append(
            Finding(
                type="recurring",
                merchant=s.merchant,
                merchant_id=s.merchant_id,
                amount_inr=s.last.amount,
                cadence=s.cadence,
                annual_impact_inr=s.annual,
                next_renewal=None if lapsed else s.next_renewal(),
                confidence=conf * (0.5 if lapsed else 1),
                explanation=(
                    f"{s.merchant} charges ₹{s.last.amount:,.0f} {s.cadence} "
                    f"({n} charge{'s' if n > 1 else ''} seen"
                    f"{', last ' + str(s.last.txn_date) if lapsed else ''})."
                ),
                evidence=s.refs(last_n=3) + [r for e in s.events[-1:] for r in _event_ref(e)],
                suggested_action="review",
                account=s.account,
            ).with_id(s.account or "")
        )
    return out


def _event_ref(e: dict) -> list:  # type: ignore[type-arg]
    from leak.findings import EvidenceRef

    return [EvidenceRef(evidence_id=e["evidence_id"], note=f"{e['kind']} email")]
