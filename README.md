# Leak

**An open-source, local-first AI agent that reads your email, bank statements and SMS alerts, finds the money quietly leaking out — forgotten subscriptions, price hikes, trials that turned paid, duplicates, charges after you cancelled, forex fees — and drafts the cancellation emails for you to approve.**

India-first (₹, HDFC/ICICI/SBI/Axis, UPI autopay, bank SMS), region-agnostic by design. The agent harness is built by hand — no LangChain, LlamaIndex or CrewAI — and is meant to be read.

[![Leak: privacy masking, findings, evidence, approvals — click for the 49-second walkthrough](docs/assets/demo.gif)](docs/assets/leak-demo.mp4)

![₹24,635/year found on the synthetic demo inbox](docs/assets/dashboard.png)

<sub>All screenshots and the video use the synthetic demo dataset (made-up persona, accounts and numbers).</sub>

```text
$ uvx leak-audit demo
 1  cult.fit       zombie: charged 3× after you cancelled     ₹17,988/yr   dispute
 2  JioHotstar     duplicate: already in your Airtel Black     ₹3,588/yr   cancel
 3  Google One     duplicate: billed on two accounts           ₹1,560/yr   cancel
 4  Amazon Prime   duplicate: already in your Airtel Black     ₹1,499/yr   cancel
 …  Netflix price hike (+₹1,800/yr), ChatGPT forex fees (₹867/yr), recurring subscriptions to review

₹24,635/year in potential savings — every row cites the statement line, email or SMS behind it.
```

---

## A quick tour

| Findings, with the savings number | Evidence drawer: the exact source lines |
|---|---|
| ![Findings](docs/assets/dashboard.png) | ![Evidence](docs/assets/evidence.png) |
| **Approvals: drafts are queued, never sent** | **What the LLM saw: post-redaction payloads** |
| ![Approvals](docs/assets/approvals.png) | ![What the LLM saw](docs/assets/llm.png) |

## Privacy & safety

Leak is built on the assumption that your financial data is nobody's business.

- **Local-first.** Everything — documents, ledger, findings, traces — lives in one SQLite file under `.leak/` on your machine. There is no Leak server and no telemetry.
- **Redacted before any LLM sees it.** PAN, Aadhaar (Verhoeff-checked), card numbers (Luhn-checked, full or masked), account numbers, phone numbers, emails, personal UPI IDs, addresses and your own names are replaced with tokens like `[CARD_2]` at ingestion. The token → value map stays in a local vault (`chmod 600`) so *your* dashboard can unmask.
- **Fail-closed egress guard.** Every payload is redacted again and re-scanned right before it leaves the process (retries included); if anything still looks like PII, the call is refused. A test plants a tool that leaks raw text on purpose and asserts nothing reaches the model ([`tests/test_privacy_egress.py`](tests/test_privacy_egress.py)).
- **Zero Data Retention routing, fail closed.** Every OpenRouter request, retries included, carries `provider: {data_collection: "deny", zdr: true, require_parameters: true}`. If no endpoint qualifies, the call fails; Leak never drops the constraint or picks a looser route. What the upstream provider (e.g. Google Vertex) does is governed by its own ZDR terms, a separate layer Leak can't enforce ([details](docs/openrouter.md#5-privacy-three-separate-layers)).
- **Your key, your bill.** Bring your own OpenRouter key; it lives in the OS keychain per profile. Leak has no shared or fallback key and never switches provider on an auth error.
- **See exactly what the model saw.** The trace records the post-guard payload of every step; the dashboard's *What the LLM saw* page shows it verbatim.
- **No send tool.** The agent can *queue* a Gmail draft or a calendar reminder; nothing happens until you press Approve. There is no code in this repository that sends email. Gmail access is read-only unless you opt in to `gmail.compose` (drafts).
- **Grounded findings only.** A finding is accepted only if every evidence id it cites exists and its amount appears in the cited text.
- **Generated text is untrusted too.** The model read your email, and email can carry prompt injections. Findings whose merchant name or explanation contains personal identifiers or links are rejected, and every draft and reminder is re-checked right before the Gmail call or file write. If anything looks off, the action is blocked, not sent.
- **Bring your own OAuth client.** Leak ships no Google client ID, so no third party ever holds a token to your inbox.
- **Share without leaking.** The Leak Card shows only totals, finding types and merchant names from the public catalog; unknown merchants become "A subscription", and the card is refused if its text looks personal. Leak never posts it for you.
- **Local dashboard hardening.** Binds to `127.0.0.1`, rejects non-localhost `Host` headers (DNS rebinding), sends no CORS headers, and requires a per-process token for approvals and unmasking.

Statements never leave unredacted either: for banks without a parser, Leak sends the *redacted text* of the statement, never the PDF. Scanned (image-only) PDFs are refused rather than uploaded.

## Quickstart

### Try it on synthetic data (one command)

```bash
uvx leak-audit demo
```

(Until the package is on PyPI: clone the repo and run `uv run leak demo`.)

Scans a synthetic inbox, four password-protected bank/card statements and an SMS backup, extracts receipts (replayed from a recorded cassette: ₹0, no key needed), runs the detectors, prints the report and opens the dashboard. With your own OpenRouter key stored (`leak llm key set`), the auditor agent also reviews every finding (~₹30, billed to your OpenRouter account).

### Your own data

An LLM is optional. Without an OpenRouter key (or with `[llm] provider = "none"`) everything runs on your machine: bank-plugin statement parsing, OCR of scanned PDFs and photos (PaddleOCR, local), SMS rules and the detectors. With your own key, a model on [OpenRouter](https://openrouter.ai) adds email extraction, unknown-bank statements and the auditor agent; every request is routed to Zero Data Retention endpoints only, and the model only ever receives masked text: account and card numbers, names, phones, emails and UPI IDs become tokens like `[ACCT_1]` before anything leaves, and the egress guard refuses to send a payload that still looks like PII. Statement PDFs and images are never uploaded.

Python 3.12 or 3.13 (PaddlePaddle has no wheels for newer versions yet). PaddleOCR downloads its models (~130 MB, to `~/.paddlex`) the first time it reads a scan.

```bash
git clone https://github.com/<you>/leak && cd leak
uv sync
cp config.example.toml config.toml        # model ids, budgets, your names (always masked)
uv run leak llm key set                   # your OpenRouter key → OS keychain (hidden prompt)
uv run leak llm models                    # configured models have a ZDR endpoint? (no key needed)
(cd web && npm install && npm run build)  # dashboard, once

uv run leak gmail auth                                   # see "Gmail setup" below
uv run leak scan --gmail                                 # add --limit 100 to sample first
uv run leak scan --statements ~/Downloads/statements     # PDFs, scans, photos; passwords: LEAK_PDF_PASSWORDS="a,b" in .env
uv run leak scan --sms ~/Downloads/sms-backup.xml        # Android "SMS Backup & Restore" export
uv run leak extract                                      # extract_model: receipts, renewals, price changes (cached)
uv run leak report                                       # detectors + auditor agent (reason_model) → ranked report
uv run leak serve                                        # dashboard on http://127.0.0.1:8765
```

<details>
<summary><b>LLM setup (OpenRouter, bring your own key)</b></summary>

1. Create a key at [openrouter.ai/settings/keys](https://openrouter.ai/settings/keys); give it a credit limit.
2. `uv run leak llm key set` stores it in the OS keychain (macOS Keychain, Windows Credential Locker, Secret Service/KWallet), one entry per data directory. Never in `config.toml`, `.env`, SQLite, traces or logs. `leak llm key replace | validate | remove` and `leak llm status` (masked) manage it.
3. Headless/CI/containers: export `OPENROUTER_API_KEY` from your secret store instead. A key in `.env` is refused, not used.
4. Models: `[llm] extract_model` / `reason_model` (or `OPENROUTER_MODEL_EXTRACT` / `OPENROUTER_MODEL_REASON`). `leak llm models` checks them against OpenRouter's live ZDR catalog.

Inference is billed to your OpenRouter account; Leak's budgets are a local meter and ceiling on top. Full guide, privacy layers and troubleshooting (401/402/429/routing): [`docs/openrouter.md`](docs/openrouter.md).
</details>

<details>
<summary><b>Gmail setup (bring your own OAuth client, ~5 minutes)</b></summary>

1. In [Google Cloud Console](https://console.cloud.google.com/) create a project and enable the **Gmail API**.
2. **Google Auth Platform → Branding**: fill in an app name and your email. **Audience**: *External*, keep it in **Testing**, and add your Gmail address under **Test users**. (You do not need Google verification for your own use.)
3. **Clients → Create client → Desktop app**, download the JSON to `~/.config/leak/client_secret.json`.
4. `uv run leak gmail auth`. Google will warn the app is unverified — it's yours; choose *Advanced → Go to …*.
5. To let Leak create drafts, set `[gmail] enable_actions = true` and run `leak gmail auth` again (adds `gmail.compose`).

Testing-mode tokens expire after 7 days; Leak notices and asks you to log in again. Google Workspace users can pick *Internal* instead and skip both the test-user list and the expiry.
</details>

## How the harness works

```mermaid
flowchart LR
  subgraph Ingest["Ingest (local)"]
    G[Gmail] --> R
    P[PDF / image statements<br/>pikepdf + pdfplumber<br/>PaddleOCR for scans<br/>bank plugins] --> R
    S[SMS backup XML] --> R
    R[Redactor<br/>PII → tokens<br/>vault stays local] --> DB[(SQLite)]
  end
  DB --> X[Extractor<br/>SMS rules · Flash batches<br/>content-hash cache]
  X --> L[Ledger<br/>merchant catalog · FX<br/>cross-source dedupe]
  L --> D[Detectors<br/>recurring · price hike · trial<br/>duplicate · zombie · forex]
  D --> A
  subgraph A["Auditor agent (leak/harness)"]
    direction TB
    LOOP[loop.py<br/>plan → act → observe] --> T[tools.py<br/>registry + schemas]
    LOOP --> PERM[permissions.py<br/>auto · needs_approval · forbidden]
    LOOP --> B[budget.py<br/>steps · $ · ₹]
    LOOP --> C[context.py<br/>compaction]
    LOOP --> TR[trace.py<br/>JSONL + replay]
    LOOP --> GR[grounding.py<br/>evidence checks]
    LOOP -. egress guard .-> LLM[(OpenRouter<br/>ZDR routing)]
  end
  A --> F[Findings]
  A --> Q[Approval queue]
  Q -- you approve --> ACT[Gmail draft · .ics reminder]
  F --> UI[Dashboard]
  Q --> UI
```

The whole agent is one loop ([`leak/harness/loop.py`](leak/harness/loop.py)):

```text
while True:
    budget.start_step()                       # step ceiling → BudgetExceeded
    payload = egress_guard(context)           # redact + re-scan; refuse on PII
    trace("llm_request", payload)             # exactly what left the machine
    response = llm.generate(payload, tools)   # every retry: guard + budget again
    budget.charge(response.usage)             # $ / ₹ ceiling → BudgetExceeded
    if not response.tool_calls: return response.text
    for call in response.tool_calls:
        auto           → validate args, run, feed result back
        needs_approval → enqueue for a human, tell the model it did NOT run
        forbidden      → never shown to the model; refused if called anyway
    context.compact()
```

| Component | File | What it teaches |
|---|---|---|
| Loop | [`loop.py`](leak/harness/loop.py) | Plan → tool call → observe; where every safety property is enforced in code |
| Tools | [`tools.py`](leak/harness/tools.py) | A Python function + Pydantic model becomes an LLM tool; JSON schema generated, args validated, errors fed back instead of crashing |
| Permissions | [`permissions.py`](leak/harness/permissions.py) | `auto` / `needs_approval` / `forbidden`; config can tighten, never loosen; approval queue |
| Budget | [`budget.py`](leak/harness/budget.py) | Step + cost ceilings from real token usage (cached tokens priced separately), shown in $ and ₹ |
| Context | [`context.py`](leak/harness/context.py) | What goes into the prompt each step; compaction of old tool output |
| Trace | [`trace.py`](leak/harness/trace.py) | Append-only JSONL flight recorder; `leak trace replay` pretty-prints, `ReplayProvider` re-runs a loop deterministically for $0 |
| Grounding | [`grounding.py`](leak/harness/grounding.py) | Claims must cite evidence that exists and supports them; rejections go back to the model to fix |

`leak/harness/` is 833 lines, 543 of them code — the rest is comments written for whoever reads it next. It has no dependency on any LLM SDK: providers implement a five-line `LLMProvider` protocol ([`leak/llm/provider.py`](leak/llm/provider.py)), and tests run the whole thing against a scripted fake model. `mypy --strict` clean.

The one remote provider is OpenRouter ([`leak/llm/openrouter.py`](leak/llm/openrouter.py), on the `openai` SDK). Model ids are config, not code (`config.example.toml`): extraction on `google/gemini-3.5-flash-lite`, the auditor on `google/gemini-3.1-pro-preview` at the time of writing.

## Evals

`uv run python evals/run_evals.py` — runs offline by replaying a recorded cassette (no key, $0), and in CI on every push.

| Metric | Result | Set |
|---|---|---|
| Extraction F1 (charges, renewals, price changes, cancellations) | **1.000** (42/42, 0 FP) | 22 emails + 21 SMS, labels generated with the data |
| Detection precision / recall | **1.000 / 1.000** | 18 planted findings incl. zombie, trial→paid, bundle duplicate, forex |
| Cross-source dedupe: missed merges | **0** | 155 ledger entries, 27 confirmed by 2–3 sources |
| SMS handled by deterministic rules | 21 / 21 | no LLM call |

| Cost (measured 2026-10-17 with the same models via Google's API, before the move to OpenRouter) | Tokens | Cost |
|---|---|---|
| Extraction, first run (22 emails → 3 Flash-Lite calls) | 11.3k in / 7.6k out | **$0.022 · ₹1.96** (≈ ₹0.09/email) |
| Extraction, any re-run | — | **₹0** (content-hash cache) |
| Bank statements (4 PDFs, 153 txns) | — | **₹0** (deterministic parsers) |
| Auditor agent, one `leak report` (20 steps, Pro) | 292k in (67% cached) / 8k out | **$0.33 · ₹29** |

These fixtures are synthetic and small, and were written alongside the parsers and prompt — read the scores as "no regressions", not "works on every inbox". Harder, independently written fixtures are the most valuable contribution you can make; see [`evals/README.md`](evals/README.md).

## Supported sources

| Source | How | Notes |
|---|---|---|
| HDFC Bank savings | parser `hdfc_savings` | direction from running balance |
| Axis Bank savings | parser `axis_savings` | |
| ICICI Bank credit card | parser `icici_credit_card` | foreign-currency column → forex detector |
| SBI Card | parser `sbi_card` | |
| Any other bank | LLM fallback on **redacted** text | rows must cite a line that contains their amount |
| Password-protected PDFs | `pikepdf`, decrypted in memory only | passwords via `LEAK_PDF_PASSWORDS` |
| Gmail | read-only API, incremental sync | drafts only with opt-in `gmail.compose` |
| Android SMS | "SMS Backup & Restore" XML | DLT senders only; OTPs and personal chats dropped before storage |

**Add your bank:** most parsers are ~20 lines — see [`docs/adding-a-bank.md`](docs/adding-a-bank.md). The merchant catalog ([`leak/ledger/merchants.yaml`](leak/ledger/merchants.yaml)) takes PRs too: aliases, typical prices, cancel links, and what telecom/credit-card bundles include.

## CLI

| Command | |
|---|---|
| `leak demo` | Whole pipeline on synthetic data, then the dashboard |
| `leak scan [--gmail] [--statements PATH] [--sms FILE] [--limit N]` | Ingest + redact into the local store |
| `leak extract` | Receipts, renewals, price changes, cancellations → ledger |
| `leak ledger [--merchant netflix]` | The normalized, de-duplicated ledger |
| `leak report [--no-agent]` | Detectors + auditor agent → ranked findings |
| `leak serve` | Dashboard: findings + evidence drawer, approvals, agent runs, what the LLM saw |
| `leak card [--no-names] [--out FILE]` | Shareable Leak Card (SVG): totals and catalog merchant names only, re-scanned for PII. The dashboard's *Share my Leak Card* exports PNG |
| `leak actions list \| approve ID \| reject ID` | The approval queue from the terminal |
| `leak trace list \| replay RUN \| rerun RUN` | Inspect or deterministically re-run any agent run |
| `leak llm key set \| replace \| validate \| remove` | Your OpenRouter key in the OS keychain (hidden prompt, masked output) |
| `leak llm status \| models` | Key source, models and routing; check models against OpenRouter's ZDR catalog |

## Development

```bash
uv sync && uv run pytest -q          # offline: no key, no network, mocked OpenRouter
uv run ruff check . && uv run mypy leak
uv run python evals/run_evals.py     # add --live to record new model output
(cd web && npm run dev)              # dashboard with hot reload, proxies /api to `leak serve`
(cd video && npm install && npm run shots && npm run render)   # screenshots + Remotion video, see video/README.md
uv run python demo/generate_sample_data.py   # regenerate synthetic data + labels
```

## Roadmap

- **Account Aggregator** (RBI AA framework) as a consented, structured alternative to PDFs and SMS
- **More banks**: Kotak, Yes, IDFC FIRST, IndusInd, AU, HSBC, Amex India, OneCard; Paytm/PhonePe wallet exports
- **Ollama-only mode**: same `LLMProvider` protocol, fully offline extraction and auditing
- **WhatsApp / Telegram bot** for the monthly "here's what leaked" summary and one-tap approvals
- **"Unused" detector**: no product-usage emails in 90 days for a paid service
- Encrypted vault (OS keychain), UPI mandate list import

## License

MIT — see [LICENSE](LICENSE). Security reports: [SECURITY.md](SECURITY.md).
