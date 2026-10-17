"""HDFC Bank savings account statement.

Row:  05/04/26  UPI-NETFLIX-netflix@hdfcbank-...  0000612345678901  05/04/26  649.00  1,24,351.00
      date      narration                          chq/ref no        value dt  amount  balance
"""

import re

from leak.ingest.banks.base import LineParser


class HdfcSavings(LineParser):
    name = "hdfc_savings"
    bank = "HDFC Bank"
    detect_all = ("HDFC BANK", "Statement of account")
    account_re = re.compile(r"Account No\s*:\s*(\d{9,18})")
    date_formats = ("%d/%m/%y",)
    line_re = re.compile(
        r"^(?P<date>\d{2}/\d{2}/\d{2})\s+(?P<desc>.+?)\s+(?P<ref>\d{10,20})\s+"
        r"\d{2}/\d{2}/\d{2}\s+(?P<amount>[\d,]+\.\d{2})\s+(?P<balance>[\d,]+\.\d{2})$"
    )
