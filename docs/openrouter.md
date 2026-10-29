# LLM setup: OpenRouter, bring your own key

Leak runs fully locally without any key: bank-plugin statement parsing, OCR, SMS rules and
the detectors need no LLM. An LLM adds email extraction, statements from banks without a
parser, and the auditor agent. For those, Leak talks to exactly one gateway,
[OpenRouter](https://openrouter.ai), with **your own** API key. Leak ships no key, has no
shared or fallback key, and never switches provider on its own. Inference is billed to your
OpenRouter account.

## 1. Create a key

1. Sign in at <https://openrouter.ai> and add credits (Settings → Credits).
2. Settings → Keys → **Create key**. Give it a name like `leak` and, ideally, a **credit
   limit** (e.g. $5). That limit is enforced by OpenRouter, on top of Leak's own budgets.
3. Settings → Privacy: you can also turn on account-wide ZDR there. Leak asks for it on every
   request anyway (see below), so this is belt and braces.

Keys look like `sk-or-v1-…`.

## 2. Store it in your OS keychain

```bash
uv run leak llm key set        # prompts, input hidden; checks the key with GET /key first
uv run leak llm status         # masked key, where it comes from, models, routing
```

The key goes into the OS keychain through [`keyring`](https://pypi.org/project/keyring/):
macOS Keychain, Windows Credential Locker, or Secret Service (gnome-keyring) / KWallet on
Linux. It is stored under service `leak-openrouter`, account `profile:<hash>`, where the hash
identifies the profile's data directory (`[paths] data_dir`). Separate data directories are
separate profiles with separate keys; one profile never falls back to another's key.

Leak never writes the key to `config.toml`, SQLite, traces or logs, and never reads it from
`.env`. If it finds `OPENROUTER_API_KEY` in a `.env` file it refuses to use it and tells you
so. If no secure keychain is available (e.g. a Linux server with no Secret Service, or a
plaintext `keyrings.alt` backend), `leak llm key set` fails and points you to the env-var
route below instead of writing a file.

The key is never passed on the command line (shell history, `ps`). For scripts you can pipe
it in: `pass show openrouter | uv run leak llm key set`.

| Task | Command |
|---|---|
| Store a key | `leak llm key set` |
| Rotate it | `leak llm key replace` (old key is overwritten; revoke it on openrouter.ai) |
| Check it | `leak llm key validate` (calls `GET /key`: label, limit, usage; sends no prompt or data) |
| Delete it | `leak llm key remove` |
| What's in use | `leak llm status` |

`--no-validate` on `set`/`replace` stores the key without the network check.

Every LLM run builds a new client from the key as it is *at that moment*: after `replace`
or `remove` the next run uses the new key or none. Nothing caches a key in memory across runs.

## 3. Headless, CI and containers

Where there is no keychain, give the key to the process through the environment from your
secret store:

```bash
export OPENROUTER_API_KEY="$(your-secret-manager get openrouter)"   # or a CI secret
uv run leak report
```

`OPENROUTER_API_KEY` in the real environment wins over the keychain. It applies to the whole
process, so don't use it to drive several profiles from one process. CI doesn't need it at
all: tests and evals run offline (see below).

## 4. Models

```toml
[llm]
provider = "openrouter"                          # or "none": fully local, key or not
extract_model = "google/gemini-3.5-flash-lite"   # structured JSON output
reason_model  = "google/gemini-3.1-pro-preview"  # tool calling (auditor agent)
timeout_s = 120                                   # per HTTP attempt
max_retries = 4                                   # per call
```

`OPENROUTER_MODEL_EXTRACT` and `OPENROUTER_MODEL_REASON` override these (environment or
`.env`).

### Model compatibility

Leak's routing (next section) only allows endpoints that have **Zero Data Retention**, do not
collect data, and support every parameter the request uses. So a model works only if at
least one of its ZDR endpoints supports:

* `extract_model`: structured outputs (`response_format` / `structured_outputs`)
* `reason_model`: tool calling (`tools`)

Not every model has such an endpoint. Check your choice against the live catalog (public
endpoints, no key and no data sent):

```bash
uv run leak llm models
```

Checked against <https://openrouter.ai/api/v1/endpoints/zdr> on 2026-10-29:

| Model | ZDR endpoints with tools + structured output | Used for |
|---|---|---|
| `google/gemini-3.5-flash-lite` | Google Vertex | default `extract_model` |
| `google/gemini-3.1-pro-preview` | Google Vertex | default `reason_model` |
| `google/gemini-3.6-flash` | Google Vertex | extraction alternative |
| `anthropic/claude-sonnet-5.5` | Google Vertex (Bedrock: tools only) | reasoning alternative |
| `openai/gpt-5.6-terra` | Azure | reasoning alternative |

Catalogs change; re-run `leak llm models` rather than trusting this table.

An unknown id, or a bare id without `vendor/` (e.g. an old `gemini-3.5-flash-lite`), is an
error before any request is made.

## 5. Privacy: three separate layers

Each layer has a different owner and different guarantees. None of them replaces the others.

**1. Leak (on your machine).** Before *every* HTTP attempt (the first try, every retry and
the one JSON re-ask), the payload is redacted again and re-scanned by the egress guard; if
anything still looks like PII the request is refused and nothing is sent. Only masked text
leaves (`[CARD_1]`, `[ACCT_2]`, …); the vault that maps tokens back stays local. Statement
PDFs and images are never uploaded. Leak does not log prompts, responses or headers; the
`openai` SDK's debug logging is forced off. Traces (local JSONL) contain the post-redaction
payload, never the key.

**2. OpenRouter routing (what Leak asks for).** Every request, retries included, carries:

```json
"provider": {"data_collection": "deny", "zdr": true, "require_parameters": true}
```

* `zdr: true`: only Zero Data Retention endpoints.
* `data_collection: "deny"`: only providers that don't store or train on your data.
* `require_parameters: true`: only endpoints that support tools / structured output as sent,
  so a request can't be quietly routed to an endpoint that ignores the schema.

This is a constant in [`leak/llm/openrouter.py`](../leak/llm/openrouter.py), not a setting.
If no endpoint satisfies it, OpenRouter answers with an error ("no endpoints found…" 404, or
a 503 about routing requirements) and Leak **fails closed**: the step stops with
`PrivacyRoutingUnavailable`. It never retries without the parameter and never picks a less
restricted route; a 503 is retried a few times *with the same constraints*, then fails.

**3. The upstream provider (e.g. Google Vertex).** The model runs at whichever provider
OpenRouter routes to. Its retention is governed by that provider's terms and its ZDR
agreement with OpenRouter, which Leak can't verify or enforce. The trace's `served_by` field
records which provider OpenRouter reported for each call. Read OpenRouter's
[data policies](https://openrouter.ai/docs/features/privacy-and-logging) and the provider's.

## 6. Cost: your bill vs Leak's meter

* **The bill** is what your OpenRouter account shows. Set a credit limit on the key.
* **Leak's meter** enforces `[budget]` / `[agent]` ceilings per run (steps, retries, USD).
  For each call it uses the cost OpenRouter reports in `usage.cost`. If a response reports
  no cost, it estimates from `[llm.prices]`, which is deliberately conservative (the priciest
  ZDR endpoint). If there is no cost and no price, the run stops instead of counting the call
  as free; with no `usage` at all, tokens are estimated from the text length.
* Before every request (retries too) Leak checks that budget remains. Retries count against
  a per-run `max_retries` ceiling. Usage from a call that later failed (e.g. a JSON re-ask
  that came back invalid) is still charged.
* `leak report` prints both: what Leak metered and how much of it OpenRouter reported.

## 7. Reliability

* Finite timeouts per attempt (`timeout_s`, default 120 s; 10 s to connect).
* Retried, with exponential backoff and full jitter (capped at 30 s), honouring
  `Retry-After` (up to 60 s; longer waits fail rather than block): 408, 429, 500, 502, 503,
  504, network errors, timeouts, a 200 with an error body or no choices.
* Never retried: 400, 401, 402, 403, 404, unknown model, context too long, malformed tool
  arguments.
* A failed call is never an empty success: empty answers, truncated answers, errors inside
  a 200 and JSON that fails validation twice all raise. No findings or records come from a
  failed call.

There is no streaming: nothing in Leak shows partial output.

## 8. Tests without a key

```bash
uv run pytest -q                      # no key, no network: HTTP is mocked
uv run python evals/run_evals.py      # replays evals/cassettes/, $0
uv run leak demo --no-agent           # whole pipeline on synthetic data, no key
```

The test suite replaces the OS keychain with an in-memory one and drops any
`OPENROUTER_API_KEY`, so it can't touch your real key. The extraction cassette
(`evals/cassettes/extract-google--gemini-3.5-flash-lite.jsonl`) was recorded on 2026-10-17
with `gemini-3.5-flash-lite` through Google's API and re-keyed for the OpenRouter id of the
same model. `run_evals.py --live` re-records cache misses with your key (synthetic data only).

## 9. Troubleshooting

| You see | Meaning | Fix |
|---|---|---|
| `auditor agent skipped: no OpenRouter key for this profile` | No key in the keychain for this data dir, no env var | `leak llm key set`; check `leak llm status` (right data dir?) |
| `OpenRouter rejected the API key (401…)` | Key invalid, disabled or revoked | `leak llm key validate`, then `leak llm key replace` |
| `insufficient credits or the key's spend limit was reached (402)` | Out of credits or key limit hit | Add credits / raise the key limit on openrouter.ai. Not retried |
| `OpenRouter rate limit (429)` | Too many requests | Retried with backoff; if it persists, wait or lower volume (`--limit`) |
| `No OpenRouter endpoint for '…' satisfies Leak's privacy routing…` | No ZDR / no-data-collection endpoint with the needed features is available | `leak llm models`; choose a compatible model. Leak will not relax routing |
| `OpenRouter does not know model '…'` | Typo or retired id | Fix `[llm]` / `OPENROUTER_MODEL_*`; see <https://openrouter.ai/models> |
| `prompt is too long for '…'` | Context window exceeded | A model with a larger context, or smaller batches |
| `OpenRouter request timed out` | Slow upstream | Retried; raise `[llm] timeout_s` for long auditor steps |
| `no secure OS keychain is available` | No usable `keyring` backend | Install/unlock gnome-keyring or KWallet, or use `OPENROUTER_API_KEY` from a secret store |
| `OPENROUTER_API_KEY is set in a .env file` | Leak won't read keys from plaintext files | Remove it from `.env`; `leak llm key set` |
| `provider "gemini" was removed` | Old config | `provider = "openrouter"`, model ids `google/<id>`, `leak llm key set` |

## Migrating from the Gemini integration

1. In `config.toml`: `provider = "openrouter"`, and prefix model ids with `google/`
   (`google/gemini-3.5-flash-lite`, `google/gemini-3.1-pro-preview`); rename the
   `[llm.prices."…"]` tables the same way (or copy them from `config.example.toml`).
2. Delete `GEMINI_API_KEY` from `.env` and revoke it in Google AI Studio if unused.
3. `uv run leak llm key set`, then `uv run leak llm models`.

Extraction cache entries are keyed by model id, so the first `leak extract` after switching
re-extracts once.
