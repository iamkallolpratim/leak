"""Render the ranked findings report."""

from __future__ import annotations

from decimal import Decimal

from rich.console import Console
from rich.table import Table

from leak.findings import SAVING_ACTIONS, Finding, potential_savings
from leak.store import Store

ACTION_STYLE = {
    "cancel": "red",
    "dispute": "red",
    "downgrade": "yellow",
    "review": "cyan",
    "keep": "dim",
}


def report_findings(store: Store) -> list[Finding]:
    """Agent findings + detector findings it didn't supersede or dismiss, ranked
    actionable-first then by annual ₹ impact."""
    active = [f for f, _, _ in store.findings("active")]
    return sorted(
        active,
        key=lambda f: (
            f.suggested_action not in SAVING_ACTIONS,
            f.suggested_action == "keep",
            -f.annual_impact_inr,
        ),
    )


def recurring_spend(store: Store) -> Decimal:
    """Annual ₹ of recurring charges the detectors found.

    Counted from the detectors' output regardless of status: when the auditor
    dismisses a plain subscription ("not a leak") it is still money you spend
    every year, so the tile must not drop to ₹0 because of that decision."""
    return sum(
        (
            Decimal(f.annual_impact_inr)
            for f, _, _ in store.findings(status=None)
            if f.source == "detector" and f.type == "recurring"
        ),
        Decimal(0),
    )


def render(store: Store, console: Console, *, show_evidence: int = 2) -> Decimal:
    findings = report_findings(store)
    t = Table(title="Leak report", show_lines=False)
    for col, kw in [
        ("#", {"justify": "right"}),
        ("merchant", {}),
        ("finding", {}),
        ("₹/charge", {"justify": "right"}),
        ("cadence", {}),
        ("₹/year", {"justify": "right"}),
        ("action", {}),
        ("conf", {"justify": "right"}),
        ("evidence", {}),
    ]:
        t.add_column(col, **kw)  # type: ignore[arg-type]
    for i, f in enumerate(findings, 1):
        ev = [e.evidence_id for e in f.evidence]
        shown = ", ".join(ev[:show_evidence]) + (
            f" +{len(ev) - show_evidence}" if len(ev) > show_evidence else ""
        )
        style = ACTION_STYLE[f.suggested_action]
        t.add_row(
            str(i),
            f.merchant.split(" (")[0],
            f.type.replace("_", " ") + ("" if f.source == "agent" else " *"),
            f"{f.amount_inr:,.0f}",
            f.cadence or "-",
            f"{f.annual_impact_inr:,.0f}",
            f"[{style}]{f.suggested_action}[/]",
            f"{f.confidence:.2f}",
            shown,
        )
    console.print(t)
    for i, f in enumerate(findings, 1):
        if f.suggested_action in SAVING_ACTIONS:
            console.print(f"  [bold]{i}.[/] {f.explanation}")
    savings = potential_savings(findings)
    review = sum(
        (
            Decimal(f.annual_impact_inr)
            for f in findings
            if f.suggested_action == "review" and f.type != "recurring"
        ),
        Decimal(0),
    )
    recurring = recurring_spend(store)
    console.print(
        f"\n[bold green]₹{savings:,.0f}/year in potential savings[/] "
        "(cancel / downgrade / dispute; counted once per merchant)"
    )
    console.print(
        f"₹{review:,.0f}/year more in leaks to review · "
        f"₹{recurring:,.0f}/year total recurring spend found"
    )
    if any(f.source == "detector" for f in findings):
        console.print("[dim]* = detector finding not (yet) reviewed by the auditor agent[/]")
    return savings
