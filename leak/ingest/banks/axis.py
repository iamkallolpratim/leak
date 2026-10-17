"""Axis Bank savings account statement.

Row:  05-04-2026  UPI/P2M/612345678901/GOOGLE INDIA  130.00  52,870.00  1234
      tran date   particulars                        amount  balance    init br
"""

import re

from leak.ingest.banks.base import LineParser


class AxisSavings(LineParser):
    name = "axis_savings"
    bank = "Axis Bank"
    detect_all = ("AXIS BANK", "Statement of Account")
    account_re = re.compile(r"Statement of Account No\s*:?\s*(\d{9,18})")
    date_formats = ("%d-%m-%Y",)
    line_re = re.compile(
        r"^(?P<date>\d{2}-\d{2}-\d{4})\s+(?P<desc>.+?)\s+(?P<amount>[\d,]+\.\d{2})\s+"
        r"(?P<balance>[\d,]+\.\d{2})(?:\s+\d{3,4})?$"
    )
