"""Deterministic detectors. Each is `detect(ctx, today) -> list[Finding]`.

They run before the LLM: code is cheaper, exact and reproducible; the
auditor agent then reviews, ranks and explains what they found.
"""

from datetime import date

from leak.detect import duplicates, forex, price_hike, recurring, trial_conversion, zombie
from leak.detect.common import Context, load_context
from leak.findings import Finding
from leak.store import Store

DETECTORS = [recurring, price_hike, trial_conversion, duplicates, zombie, forex]


def run_detectors(store: Store, today: date | None = None) -> list[Finding]:
    ctx = load_context(store)
    today = today or max((c.txn_date for c in ctx.charges), default=date.today())
    findings = [f for d in DETECTORS for f in d.detect(ctx, today)]
    return sorted(findings, key=lambda f: -f.annual_impact_inr)


__all__ = ["DETECTORS", "Context", "load_context", "run_detectors"]
