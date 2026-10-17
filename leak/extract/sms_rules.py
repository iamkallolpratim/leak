"""Deterministic extraction for common Indian bank SMS formats.

Most debit alerts follow a handful of shapes. Regexes handle those for free
and exactly; only SMS that match none of them go to the LLM. Input is the
*redacted* SMS text (accounts appear as [ACCT_1] / [CARD_2] tokens).
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal

from leak.extract.schemas import Channel, DocExtraction, Transaction

_AMT = r"(?:Rs\.?|INR|₹)\s?(?P<amount>[\d,]+(?:\.\d{1,2})?)"
_TOKEN = r"(?P<account>\[(?:ACCT|CARD)_\d+\])"
_DATE = r"(?P<date>\d{1,2}[-/ ](?:\d{1,2}|[A-Za-z]{3})[-/ ]\d{2,4})"

# (pattern, channel). Order matters: first match wins.
RULES: list[tuple[re.Pattern[str], Channel]] = [
    # Rs.649.00 debited from a/c [ACCT_1] on 05-04-26 to VPA netflix@hdfcbank (UPI Ref ...)
    (
        re.compile(
            rf"{_AMT} debited from a/c {_TOKEN} on {_DATE} to VPA (?P<merchant>[\w.\-@]+)", re.I
        ),
        "upi",
    ),
    # Rs.119.00 spent on your SBI Credit Card ending [CARD_3] at SPOTIFY INDIA on 12/04/26.
    (
        re.compile(
            rf"{_AMT} spent on your .*?Card ending {_TOKEN} at (?P<merchant>.+?) on {_DATE}", re.I
        ),
        "card",
    ),
    # INR 299.00 spent on ICICI Bank Card [CARD_1] on 30-Jun-26 on HOTSTAR.
    (
        re.compile(
            rf"{_AMT} spent on .*?Card {_TOKEN} on {_DATE} (?:on|at) (?P<merchant>[^.]+)", re.I
        ),
        "card",
    ),
    # ACH Debit of INR 1499.00 from A/c no. [ACCT_2] towards CULTFIT HEALTHCARE on 12-07-2026.
    (
        re.compile(
            rf"ACH Debit of {_AMT} from A/c(?: no\.?)? {_TOKEN} towards (?P<merchant>.+?) "
            rf"on {_DATE}",
            re.I,
        ),
        "ach",
    ),
]

# Messages that look financial but carry no transaction.
NOT_A_TXN = re.compile(r"(?i)\bmandate\b.*\b(created|registered|revoked)\b|\bOTP\b")

_DATE_FORMATS = ("%d-%m-%y", "%d-%m-%Y", "%d/%m/%y", "%d/%m/%Y", "%d-%b-%y", "%d %b %y")


def _parse_date(s: str) -> date | None:
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def extract_sms(evidence_id: str, text: str) -> DocExtraction | None:
    """A DocExtraction if a rule handles this SMS, else None (-> LLM)."""
    if NOT_A_TXN.search(text):
        return DocExtraction(evidence_id=evidence_id, is_financial=True)
    for pattern, channel in RULES:
        m = pattern.search(text)
        if m:
            txn = Transaction(
                txn_date=_parse_date(m["date"]),
                amount=Decimal(m["amount"].replace(",", "")),
                merchant=m["merchant"].strip().rstrip("."),
                account=m["account"],
                channel=channel,
            )
            return DocExtraction(evidence_id=evidence_id, is_financial=True, transactions=[txn])
    return None
