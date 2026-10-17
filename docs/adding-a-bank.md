# Add your bank parser

Banks without a parser still work: Leak sends their statement's **redacted text**
to a cheap model (`LLMStatementParser`), and drops any row whose cited line doesn't
contain its amount. A parser is better: free, exact, and offline. Most take ~20 lines.

## 1. Look at the text, not the PDF

Parsers see what `pdfplumber` extracts, one string per page:

```bash
uv run python -c "
from pathlib import Path
from leak.ingest.statements import unlock_pdf, pdf_pages_text
for i, page in enumerate(pdf_pages_text(unlock_pdf(Path('my_statement.pdf'), ['PASSWORD'])), 1):
    print(f'--- page {i} ---'); print(page)
"
```

This prints your real statement to your own terminal. **Never paste it into an
issue or PR**: build a synthetic fixture instead (step 4).

## 2. Write the plugin

Create `leak/ingest/banks/<bank>.py`. Any `LineParser` subclass with a `name` is
registered automatically:

```python
"""Kotak Mahindra Bank savings statement.

Row:  05-04-2026  UPI/NETFLIX/612345678901  649.00(Dr)  1,24,351.00(Cr)
"""

import re

from leak.ingest.banks.base import LineParser


class KotakSavings(LineParser):
    name = "kotak_savings"  # shows up in `leak scan` output
    bank = "Kotak Mahindra Bank"
    detect_all = ("Kotak Mahindra Bank", "Statement of Account")  # all must appear on page 1
    account_re = re.compile(r"Account No\.?\s*:?\s*(\d{9,18})")
    date_formats = ("%d-%m-%Y",)
    line_re = re.compile(
        r"^(?P<date>\d{2}-\d{2}-\d{4})\s+(?P<desc>.+?)\s+"
        r"(?P<amount>[\d,]+\.\d{2})\((?P<dc>Dr|Cr)\)\s+(?P<balance>[\d,]+\.\d{2})"
    )
```

Named groups `LineParser` understands:

| group | meaning |
|---|---|
| `date`, `desc`, `amount` | required |
| `dc` | `D`/`C`/`Dr`/`Cr` → direction |
| `cr` | present ⇒ credit (e.g. a trailing `CR` on card statements) |
| `balance` | running balance; if there's no `dc`/`cr`, direction is inferred from the balance delta (needs an "Opening Balance" line) |
| `fcur`, `famt` | foreign currency + amount on card statements (feeds the forex detector) |

Set `is_credit_card = True` for card statements so the account is tokenised as a card.
If a bank's layout isn't line-per-transaction, subclass `BankParser` and implement
`detect()` + `parse()` yourself; return `ParsedLine`s with page and line numbers,
since they become the evidence ids findings cite.

## 3. Check it

```bash
uv run leak scan --statements my_statement.pdf
uv run leak ledger -n 20
```

`parser` should read `kotak_savings`, not `llm_fallback`.

## 4. Add a synthetic fixture + test

Add a generator for your layout in `demo/generate_sample_data.py`, following
`gen_axis()` (fake persona, fake account number, Luhn-valid *test* card numbers
only), then add a row to the parametrised `test_bank_parsers` in
`tests/test_ingest.py` with the expected transaction and credit counts.

## 5. Open the PR

Include: the bank and statement type, the fixture, and the test. Do not include
real statements, screenshots of them, or real account numbers.
