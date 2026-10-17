"""Merchant catalog + deterministic resolution.

`resolve("UPI-NETFLIX-netflix@hdfcbank-6123...")` -> Merchant(id="netflix", ...).
Longest alias wins, so "swiggy one" beats "swiggy" and "igst on markup fee"
beats "markup fee". Unknown merchants get a cleaned key so identical
narrations still group together.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

CATALOG_PATH = Path(__file__).with_name("merchants.yaml")


class Merchant(BaseModel):
    id: str
    name: str
    category: str
    aliases: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    typical: dict[str, int] = Field(default_factory=dict)
    cancel: dict[str, str] = Field(default_factory=dict)
    includes: list[str] = Field(default_factory=list)


class Catalog:
    def __init__(self, merchants: list[Merchant]) -> None:
        self.by_id = {m.id: m for m in merchants}
        pairs = sorted(
            ((a.lower(), m) for m in merchants for a in m.aliases), key=lambda p: -len(p[0])
        )
        self._aliases = pairs
        self._alias_re = re.compile(
            r"(?<![a-z0-9])(" + "|".join(re.escape(a) for a, _ in pairs) + r")(?![a-z0-9])"
        )
        self._alias_map = {a: m for a, m in pairs}
        self._domains = {d.lower(): m for m in merchants for d in m.domains}

    @classmethod
    def load(cls, path: Path = CATALOG_PATH) -> Catalog:
        data: list[dict[str, Any]] = yaml.safe_load(path.read_text())
        return cls([Merchant.model_validate(d) for d in data])

    def get(self, merchant_id: str) -> Merchant | None:
        return self.by_id.get(merchant_id)

    def resolve(self, text: str, sender: str | None = None) -> Merchant | None:
        low = text.lower()
        # Normalise separators so "UPI-NETFLIX-..." and "GOOGLE *GOOGLE ONE" match.
        spaced = re.sub(r"[-_/|]+", " ", low)
        best: Merchant | None = None
        best_len = 0
        for candidate in (low, spaced):
            for m in self._alias_re.finditer(candidate):
                if len(m.group(1)) > best_len:
                    best, best_len = self._alias_map[m.group(1)], len(m.group(1))
        if best:
            return best
        if sender and (dm := re.search(r"@([\w.-]+)", sender.lower())):
            domain = dm.group(1).rstrip(">")
            parts = domain.split(".")
            for i in range(len(parts) - 1):  # account.netflix.com -> netflix.com
                if (hit := self._domains.get(".".join(parts[i:]))) is not None:
                    return hit
        return None


def resolve_extracted(
    cat: Catalog, merchant: str, plan: str | None, sender: str, subject: str
) -> Merchant | None:
    """Resolve an LLM-extracted record. Models often put the brand in `merchant`
    and the product in `plan` ("Amazon" + "Prime"), so match on both plus the
    subject line; the longest alias wins ("amazon prime" over "amazon")."""
    return cat.resolve(f"{merchant} {plan or ''} | {subject}", sender=sender)


@lru_cache(maxsize=1)
def default_catalog() -> Catalog:
    return Catalog.load()


_NOISE = re.compile(
    r"\[[a-z]+_\d+\]|\b\d{5,}\b|\b(upi|p2m|p2a|p2p|imps|neft|ach|dr|cr|nwd|billpay|autopay|"
    r"mandate|order|ref|india|in|mumbai|bangalore|bengaluru|gurgaon|delhi|pvt|ltd)\b|[^a-z ]"
)


def clean_key(text: str) -> str:
    """Fallback grouping key for merchants not in the catalog."""
    return re.sub(r"\s+", " ", _NOISE.sub(" ", text.lower())).strip()[:40] or "unknown"
