# Evals

```bash
uv run python evals/run_evals.py          # offline: replays evals/cassettes/, no key, $0
uv run python evals/run_evals.py --live   # calls Gemini for cache misses and records them
```

## What's measured

| Metric | How |
|---|---|
| **Extraction P/R/F1** per kind (`charge`, `renewal`, `price_change`, `cancellation`) | Each labelled document's expected records vs extracted ones, matched on `(kind, merchant id, amount)`. A transaction and a receipt describing the same payment count as one `charge`. Merchant ids come from the same resolver the ledger uses, so resolution errors count against extraction. |
| **Detection P/R/F1** | Deterministic detectors vs the 18 findings planted in the demo data (`fixtures/demo_findings.json`), scored on `(type, merchant id)`. Anything else they report (rent, salary, food orders…) is a false positive. |
| **Missed merges** | Ledger entries sourced only from SMS/email that have a matching statement line (same merchant, ±3 days, amount within 6%) — i.e. one charge counted twice. Should be 0. |
| **Cost per run** | Tokens and USD/₹ from the budget meter. A re-run hits the content-hash cache and costs ₹0. |

Results are written to `evals/results.json`. Measured live costs (tokens, $, ₹) are kept in
`live_costs.json`; `--live` updates the extraction entry.

## Data

* `fixtures/demo_labels.jsonl` — written by `demo/generate_sample_data.py` together with the
  synthetic demo inbox, so labels can't drift from the data. 43 docs: 22 emails + 21 SMS.
* `cassettes/extract-<model>.jsonl` — recorded model output keyed by
  `sha256(prompt version, model, redacted text)`. Changing the prompt (`PROMPT_VERSION`) or the
  model invalidates exactly the affected entries; re-record with `--live`.

Only synthetic data is ever sent to a model by the evals.

## Caveat

The fixture set is small and was written by the same people who wrote the parsers and
prompt, so a perfect score here means "no regressions", not "works on every inbox". Adding
harder, independently written fixtures (other banks' SMS formats, Hindi/Hinglish receipts,
refunds, multi-item invoices) is the most valuable contribution you can make here.
