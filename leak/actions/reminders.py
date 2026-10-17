"""Renewal reminders as standard .ics files (any calendar app imports them)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from leak.findings import Finding

LEAD_DAYS = 3


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    """RFC 5545: lines longer than 75 octets are folded with CRLF + space."""
    out, cur = [], ""
    for ch in line:
        if len((cur + ch).encode()) > 75:
            out.append(cur)
            cur = " " + ch
        else:
            cur += ch
    return "\r\n".join([*out, cur])


def renewal_ics(finding: Finding, now: datetime | None = None) -> str:
    if finding.next_renewal is None:
        raise ValueError(f"no known renewal date for {finding.merchant}")
    now = now or datetime.now(UTC)
    day = finding.next_renewal
    summary = f"{finding.merchant} renews - Rs {finding.amount_inr:,.0f}: keep or cancel?"
    desc = (
        f"{finding.explanation}\n\nAnnual impact: Rs {finding.annual_impact_inr:,.0f}. "
        f"Suggested: {finding.suggested_action}. Leak finding {finding.id}."
    )
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Leak//Renewal reminder//EN",
        "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        f"UID:{finding.id}-{day:%Y%m%d}@leak.local",
        f"DTSTAMP:{now:%Y%m%dT%H%M%SZ}",
        f"DTSTART;VALUE=DATE:{day:%Y%m%d}",
        f"DTEND;VALUE=DATE:{day + timedelta(days=1):%Y%m%d}",
        f"SUMMARY:{_esc(summary)}",
        f"DESCRIPTION:{_esc(desc)}",
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        f"DESCRIPTION:{_esc(summary)}",
        f"TRIGGER:-P{LEAD_DAYS}D",
        "END:VALARM",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(_fold(x) for x in lines) + "\r\n"
