"""The Leak Card: a shareable summary image, made on this machine.

Sharing "Leak found ₹24,635/year" is how people hear about Leak, so the card
has to be safe to post by construction, not by the user squinting at it:

* Merchant names come only from the public catalog (merchants.yaml). A finding
  whose merchant isn't in the catalog -- possibly a person, a landlord, or a
  name the model wrote -- shows as "A subscription". `names=False` hides even
  catalog names.
* Nothing else from a finding is used: no explanation, account, dates,
  evidence or per-charge amount. Only finding types and annual ₹ totals.
* The final text is re-scanned for PII and the card is refused if anything
  looks personal (fail closed, like the egress guard).

The SVG is rendered here, in Python, so the CLI and the dashboard draw the
same card and tests can assert on exactly what would be posted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from xml.sax.saxutils import escape

from leak.findings import SAVING_ACTIONS, Finding, potential_savings
from leak.ledger.merchants import Catalog, default_catalog
from leak.privacy.redact import find_pii

WIDTH, HEIGHT = 1080, 1350  # 4:5 -- full-size on Instagram, X and WhatsApp
MAX_ROWS = 4
TRY_IT = "uvx leak-audit demo"

# Plural phrases for the "3 zombie charges · 2 duplicates" line.
_KIND: dict[str, tuple[str, str]] = {
    "zombie": ("charge after cancelling", "charges after cancelling"),
    "duplicate": ("duplicate", "duplicates"),
    "price_hike": ("price hike", "price hikes"),
    "trial_conversion": ("trial turned paid", "trials turned paid"),
    "forex": ("forex fee", "forex fees"),
    "unused": ("unused plan", "unused plans"),
}
_ROW_LABEL: dict[str, str] = {
    "zombie": "Charged after I cancelled",
    "duplicate": "Paying for it twice",
    "price_hike": "Price went up",
    "trial_conversion": "Free trial turned paid",
    "forex": "Forex fees",
    "unused": "Not used in months",
    "recurring": "Subscription",
}


class CardPrivacyError(ValueError):
    """The card text looked personal; refuse to produce it."""


@dataclass
class CardRow:
    name: str
    label: str
    annual_inr: Decimal


@dataclass
class LeakCard:
    total_inr: Decimal
    kinds: list[str]
    rows: list[CardRow]
    more: int = 0
    names: bool = True
    texts: list[str] = field(default_factory=list)

    @property
    def headline(self) -> str:
        return f"{inr(self.total_inr)}/year" if self.total_inr else "No leaks found"

    def share_text(self) -> str:
        if not self.total_inr:
            return f"Leak checked my subscriptions locally: nothing leaking. Try it: {TRY_IT}"
        kinds = f" ({', '.join(self.kinds)})" if self.kinds else ""
        return (
            f"Leak found {inr(self.total_inr)}/year leaking out of my accounts{kinds}. "
            f"It runs on my machine; nothing uploaded. Try it: {TRY_IT}"
        )


def inr(amount: Decimal | float | int) -> str:
    """₹ with Indian digit grouping, whole rupees: 1234567 -> ₹12,34,567."""
    n = int(Decimal(amount).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    s = str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        head = re.sub(r"(\d)(?=(\d{2})+$)", r"\1,", head)
        s = f"{head},{tail}"
    return f"{'-' if n < 0 else ''}₹{s}"


def _public_name(f: Finding, catalog: Catalog) -> str | None:
    m = catalog.get(f.merchant_id) if f.merchant_id else None
    if m is None:
        return None
    return re.sub(r"\s*\(.*?\)", "", m.name).strip() or None  # "JioHotstar (Disney+ Hotstar)"


def build_card(
    findings: list[Finding], *, names: bool = True, catalog: Catalog | None = None
) -> LeakCard:
    catalog = catalog or default_catalog()

    # Same rule as the dashboard headline: one row per merchant, its largest
    # actionable impact, so the rows add up to the total.
    best: dict[str, Finding] = {}
    for f in findings:
        if f.suggested_action in SAVING_ACTIONS:
            k = f.merchant_id or f.merchant.lower()
            if k not in best or f.annual_impact_inr > best[k].annual_impact_inr:
                best[k] = f
    ranked = sorted(best.values(), key=lambda f: -f.annual_impact_inr)

    rows = []
    for f in ranked[:MAX_ROWS]:
        public = _public_name(f, catalog) if names else None
        label = _ROW_LABEL.get(f.type, "Subscription")
        rows.append(
            CardRow(
                name=public or (label if not names else "A subscription"),
                label=label if (public or names) else "",
                annual_inr=Decimal(f.annual_impact_inr),
            )
        )

    counts: dict[str, int] = {}
    for f in findings:
        if f.type in _KIND and f.suggested_action != "keep":
            counts[f.type] = counts.get(f.type, 0) + 1
    kinds = [f"{n} {_KIND[t][n != 1]}" for t, n in sorted(counts.items(), key=lambda kv: -kv[1])][
        :3
    ]

    card = LeakCard(
        total_inr=potential_savings(findings),
        kinds=kinds,
        rows=rows,
        more=max(0, len(ranked) - MAX_ROWS),
        names=names,
    )
    card.texts = [card.headline, card.share_text(), *kinds]
    card.texts += [t for r in rows for t in (r.name, r.label)]
    _vet(card.texts)
    return card


def _vet(texts: list[str]) -> None:
    for t in texts:
        if hits := find_pii(t):
            raise CardPrivacyError(f"card text looks personal ({hits[0].kind}); not rendering")
        if re.search(r"https?://|www\.|@", t):
            raise CardPrivacyError("card text contains a link or address; not rendering")


def render_svg(card: LeakCard) -> str:
    """A self-contained SVG: no scripts, no external fonts or images."""
    e = escape
    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" '
        f'viewBox="0 0 {WIDTH} {HEIGHT}" font-family="system-ui, -apple-system, '
        f"'Segoe UI', Roboto, 'Noto Sans', sans-serif\">",
        "<defs>"
        '<linearGradient id="bg" x1="0" y1="0" x2="0.4" y2="1">'
        '<stop offset="0" stop-color="#0f2a1e"/><stop offset="1" stop-color="#0a1218"/>'
        "</linearGradient>"
        '<radialGradient id="glow" cx="0.2" cy="0.25" r="0.6">'
        '<stop offset="0" stop-color="#2fd17f" stop-opacity="0.22"/>'
        '<stop offset="1" stop-color="#2fd17f" stop-opacity="0"/>'
        "</radialGradient>"
        "</defs>",
        f'<rect width="{WIDTH}" height="{HEIGHT}" fill="url(#bg)"/>',
        f'<rect width="{WIDTH}" height="{HEIGHT}" fill="url(#glow)"/>',
        # brand
        '<rect x="80" y="80" width="64" height="64" rx="14" fill="#2fd17f"/>',
        '<text x="112" y="126" text-anchor="middle" font-size="38" font-weight="700" '
        'fill="#0a1218">₹</text>',
        '<text x="164" y="126" font-size="40" font-weight="700" fill="#f2f5f3">Leak</text>',
        '<text x="1000" y="124" text-anchor="end" font-size="28" fill="#9fb3aa">'
        "🔒 found on my laptop</text>",
    ]

    if card.total_inr:
        out += [
            '<text x="80" y="300" font-size="48" fill="#9fb3aa">Leak found</text>',
            f'<text x="72" y="440" font-size="{_fit(inr(card.total_inr), 150, 900)}" '
            f'font-weight="800" fill="#4ee49a" letter-spacing="-3">'
            f"{e(inr(card.total_inr))}</text>",
            '<text x="80" y="510" font-size="44" fill="#f2f5f3">a year, leaking out of my '
            "accounts</text>",
        ]
    else:
        out += [
            '<text x="80" y="330" font-size="48" fill="#9fb3aa">Leak checked my accounts</text>',
            '<text x="72" y="460" font-size="130" font-weight="800" fill="#4ee49a" '
            'letter-spacing="-3">All clear</text>',
            '<text x="80" y="530" font-size="44" fill="#f2f5f3">nothing leaking right now</text>',
        ]

    if card.kinds:
        out.append(
            f'<text x="80" y="590" font-size="32" fill="#ffc861">{e(" · ".join(card.kinds))}</text>'
        )

    y = 680
    for r in card.rows:
        out.append(
            f'<rect x="64" y="{y}" width="952" height="112" rx="20" fill="#ffffff" '
            'fill-opacity="0.06"/>'
        )
        if r.label:
            out += [
                f'<text x="104" y="{y + 50}" font-size="40" font-weight="650" fill="#f2f5f3">'
                f"{e(_clip(r.name, 28))}</text>",
                f'<text x="104" y="{y + 88}" font-size="28" fill="#9fb3aa">{e(r.label)}</text>',
            ]
        else:
            out.append(
                f'<text x="104" y="{y + 68}" font-size="38" font-weight="650" fill="#f2f5f3">'
                f"{e(_clip(r.name, 28))}</text>"
            )
        out.append(
            f'<text x="976" y="{y + 68}" text-anchor="end" font-size="40" font-weight="700" '
            f'fill="#4ee49a">{e(inr(r.annual_inr))}<tspan font-size="26" fill="#9fb3aa" '
            'font-weight="400">/yr</tspan></text>'
        )
        y += 128
    if card.more:
        out.append(
            f'<text x="104" y="{y + 30}" font-size="30" fill="#9fb3aa">+ {card.more} more</text>'
        )

    out += [
        '<line x1="80" y1="1180" x2="1000" y2="1180" stroke="#ffffff" stroke-opacity="0.12"/>',
        '<text x="80" y="1240" font-size="30" fill="#9fb3aa">Open source · runs locally · '
        "no bank login, nothing uploaded</text>",
        '<text x="80" y="1290" font-size="32" fill="#f2f5f3">Try it: <tspan '
        'font-family="ui-monospace, \'SF Mono\', Menlo, monospace" fill="#4ee49a">'
        f"{e(TRY_IT)}</tspan></text>",
        "</svg>",
    ]
    return "\n".join(out)


def _fit(text: str, size: int, max_width: int) -> int:
    """Shrink the headline so long totals (₹12,34,567) stay on the card."""
    approx = len(text) * size * 0.58
    return size if approx <= max_width else max(80, int(size * max_width / approx))


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"
