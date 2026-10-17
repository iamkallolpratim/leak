"""SBI Card credit card statement.

Row:  12 Apr 26  SPOTIFY INDIA MUMBAI  119.00 D
      date       transaction details   amount D(ebit)/C(redit)
"""

import re

from leak.ingest.banks.base import LineParser


class SbiCard(LineParser):
    name = "sbi_card"
    bank = "SBI Card"
    is_credit_card = True
    detect_all = ("SBI Card", "Credit Card Number")
    account_re = re.compile(r"Credit Card Number\s*:?\s*([\dX ]{16,19}\d)")
    date_formats = ("%d %b %y",)
    line_re = re.compile(
        r"^(?P<date>\d{2} [A-Z][a-z]{2} \d{2})\s+(?P<desc>.+?)\s+"
        r"(?P<amount>[\d,]+\.\d{2})\s+(?P<dc>[DC])$"
    )
