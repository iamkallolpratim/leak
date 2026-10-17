"""ICICI Bank credit card statement.

Row:  07/04/2026  11223344556  NETFLIX.COM MUMBAI IN               12  649.00
      07/04/2026  11223344557  OPENAI *CHATGPT SAN FRANCISCO US  USD 20.00  0  1,712.40
      date        ser no       details                [intl amt]  points  amount [CR]
"""

import re

from leak.ingest.banks.base import LineParser


class IciciCreditCard(LineParser):
    name = "icici_credit_card"
    bank = "ICICI Bank"
    is_credit_card = True
    detect_all = ("ICICI Bank", "Credit Card")
    account_re = re.compile(r"Card Number\s*:?\s*([\dX ]{16,19}\d)")
    date_formats = ("%d/%m/%Y",)
    line_re = re.compile(
        r"^(?P<date>\d{2}/\d{2}/\d{4})\s+(?P<ser>\d{8,12})\s+(?P<desc>.+?)\s+"
        r"(?:(?P<fcur>[A-Z]{3})\s+(?P<famt>[\d,]+\.\d{2})\s+)?(?P<pts>-?\d+)\s+"
        r"(?P<amount>[\d,]+\.\d{2})(?:\s+(?P<cr>CR))?$"
    )
