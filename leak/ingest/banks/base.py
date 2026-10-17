"""Bank statement parser plugin base.

Adding a bank = one file in this directory with one subclass. Most Indian
statements are "one transaction per text line", so `LineParser` does the
work: give it a line regex with named groups and it handles dates, amounts,
direction and page/line bookkeeping (needed for evidence citations).

Named groups understood by LineParser.line_re:
    date, desc, amount           required
    balance                      running balance (enables direction-by-delta)
    cr                           present => credit (e.g. trailing "CR")
    dc                           "D" / "C" (or "Dr" / "Cr")
    fcur, famt                   foreign currency code + amount
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import ClassVar, Literal

Direction = Literal["debit", "credit"]


@dataclass
class ParsedLine:
    page: int  # 1-based
    line: int  # 1-based line number within the page's extracted text
    txn_date: date
    description: str
    amount: Decimal
    direction: Direction
    balance: Decimal | None = None
    foreign_amount: Decimal | None = None
    foreign_currency: str | None = None


@dataclass
class ParsedStatement:
    bank: str
    parser: str
    is_credit_card: bool
    account_hint: str | None  # raw account/card number as printed (redacted later)
    lines: list[ParsedLine] = field(default_factory=list)


def parse_amount(s: str) -> Decimal:
    return Decimal(s.replace(",", "").replace("₹", "").strip())


class BankParser(ABC):
    name: ClassVar[str]
    bank: ClassVar[str]
    is_credit_card: ClassVar[bool] = False
    registry: ClassVar[list[type[BankParser]]] = []

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if "name" in cls.__dict__:  # concrete parsers declare a name
            BankParser.registry.append(cls)

    @classmethod
    @abstractmethod
    def detect(cls, first_page: str) -> bool: ...

    @abstractmethod
    def parse(self, pages: list[str]) -> ParsedStatement: ...


class LineParser(BankParser):
    line_re: ClassVar[re.Pattern[str]]
    account_re: ClassVar[re.Pattern[str]]
    date_formats: ClassVar[tuple[str, ...]]
    detect_all: ClassVar[tuple[str, ...]]  # all must appear (case-insensitive)
    opening_re: ClassVar[re.Pattern[str]] = re.compile(
        r"(?i)opening\s+balance\s*:?\s*([\d,]+\.\d{2})"
    )

    @classmethod
    def detect(cls, first_page: str) -> bool:
        low = first_page.lower()
        return all(k.lower() in low for k in cls.detect_all)

    def _date(self, s: str) -> date:
        for fmt in self.date_formats:
            try:
                return datetime.strptime(s.strip(), fmt).date()
            except ValueError:
                continue
        raise ValueError(f"{self.name}: unparseable date {s!r}")

    def parse(self, pages: list[str]) -> ParsedStatement:
        full = "\n".join(pages)
        acct = self.account_re.search(full)
        opening = self.opening_re.search(full)
        prev_balance = parse_amount(opening.group(1)) if opening else None
        out = ParsedStatement(
            bank=self.bank,
            parser=self.name,
            is_credit_card=self.is_credit_card,
            account_hint=acct.group(1).strip() if acct else None,
        )
        for p, page in enumerate(pages, start=1):
            for n, raw in enumerate(page.splitlines(), start=1):
                m = self.line_re.match(raw.strip())
                if not m:
                    continue
                g = m.groupdict()
                amount = parse_amount(g["amount"])
                balance = parse_amount(g["balance"]) if g.get("balance") else None
                out.lines.append(
                    ParsedLine(
                        page=p,
                        line=n,
                        txn_date=self._date(g["date"]),
                        description=re.sub(r"\s+", " ", g["desc"]).strip(),
                        amount=amount,
                        direction=self._direction(g, balance, prev_balance),
                        balance=balance,
                        foreign_amount=parse_amount(g["famt"]) if g.get("famt") else None,
                        foreign_currency=g.get("fcur"),
                    )
                )
                prev_balance = balance if balance is not None else prev_balance
        return out

    def _direction(
        self, g: dict[str, str | None], balance: Decimal | None, prev: Decimal | None
    ) -> Direction:
        if "cr" in g:
            return "credit" if g["cr"] else "debit"
        if dc := g.get("dc"):
            return "credit" if dc.upper().startswith("C") else "debit"
        # Savings statements print debit and credit in separate columns, which
        # text extraction flattens. The running balance tells us which it was.
        if balance is not None and prev is not None:
            return "credit" if balance > prev else "debit"
        raise ValueError(f"{self.name}: cannot infer debit/credit (no opening balance)")
