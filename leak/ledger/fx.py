"""FX rates to INR, offline-first.

Card statements already show the INR amount actually billed, so FX here is
only for amounts we know solely in a foreign currency (e.g. "$20.00" in an
email receipt) and for estimating markup. Rates are *approximate quarterly
averages*, good enough to match an email receipt to a statement line and to
size annual impact -- not for accounting. Quarters past the table's end use
the last known value and are flagged `approximate=True`.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel

# currency -> {(year, quarter): INR per unit}
_TABLE: dict[str, dict[tuple[int, int], str]] = {
    "USD": {(2024, 1): "83.0", (2024, 2): "83.4", (2024, 3): "83.8", (2024, 4): "84.5",
            (2025, 1): "86.6", (2025, 2): "85.6", (2025, 3): "87.5", (2025, 4): "88.5"},
    "EUR": {(2024, 1): "90.2", (2024, 2): "89.8", (2024, 3): "92.3", (2024, 4): "89.6",
            (2025, 1): "90.6", (2025, 2): "96.5", (2025, 3): "102.0", (2025, 4): "103.0"},
    "GBP": {(2024, 1): "105.4", (2024, 2): "105.6", (2024, 3): "109.5", (2024, 4): "108.0",
            (2025, 1): "109.0", (2025, 2): "114.0", (2025, 3): "117.5", (2025, 4): "116.0"},
}  # fmt: skip


class FxRate(BaseModel):
    currency: str
    on: date
    inr_per_unit: Decimal
    approximate: bool  # True when outside the table and we extrapolated


def fx_rate(currency: str, on: date) -> FxRate:
    cur = currency.upper()
    if cur == "INR":
        return FxRate(currency=cur, on=on, inr_per_unit=Decimal(1), approximate=False)
    table = _TABLE.get(cur)
    if table is None:
        raise KeyError(f"no FX data for {cur}")
    q = (on.year, (on.month - 1) // 3 + 1)
    if q in table:
        return FxRate(currency=cur, on=on, inr_per_unit=Decimal(table[q]), approximate=False)
    keys = sorted(table)
    nearest = keys[-1] if q > keys[-1] else keys[0]
    return FxRate(currency=cur, on=on, inr_per_unit=Decimal(table[nearest]), approximate=True)


def to_inr(amount: Decimal, currency: str, on: date) -> Decimal:
    return (amount * fx_rate(currency, on).inr_per_unit).quantize(Decimal("0.01"))
