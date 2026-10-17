"""Batched LLM extraction with a content-hash cache.

    documents ─┬─ SMS matching a regex rule ───────────────> DocExtraction  (free)
               ├─ cache hit (same redacted text+prompt+model) > DocExtraction  (free)
               └─ the rest, batched N per call ─> Flash ───> DocExtraction  (paid, cached)

Cache key = sha256(prompt version, model, redacted text). Re-running a scan
therefore costs ~₹0; changing the prompt or model invalidates exactly what it
should. Every record is grounded: its amount(s) must literally appear in the
document, or the record is dropped (and counted).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Protocol

import sqlite_utils
from pydantic import BaseModel

from leak.extract.schemas import BatchExtraction, DocExtraction, Record
from leak.extract.sms_rules import extract_sms
from leak.harness.budget import Budget, BudgetExceeded
from leak.ingest.models import Document
from leak.ingest.progress import NullProgress, Progress
from leak.llm.provider import LLMError, LLMProvider, Message, Usage
from leak.privacy.guard import EgressGuard, GuardedProvider
from leak.privacy.redact import Redactor

PROMPT_VERSION = "extract-v1"
MAX_DOC_CHARS = 4000

SYSTEM = """You extract money-related facts from emails and SMS for a personal
subscription auditor. Personal data is masked as tokens like [CARD_1]; copy tokens
verbatim, never guess what they hide.

For each <doc>, return one entry with its exact id and:
- transactions: money that actually moved (bank/card alerts, "we charged you").
- receipts: a merchant's receipt/invoice/bill for a payment (one per payment).
- renewals: an *upcoming* charge (renewal reminder, trial ending, premium due).
- price_changes: an announced price change.
- cancellations: confirmation that something was cancelled.
Rules: amounts are plain numbers in the stated currency (USD stays USD). Dates are
YYYY-MM-DD. cadence is the billing period if stated or obvious. Do not invent
values; use null when unknown. Newsletters/marketing/OTP: is_financial=false and
empty lists. A receipt that mentions the next billing date is still just a receipt."""


# --- cache -------------------------------------------------------------------------


class ExtractionCache(Protocol):
    def get(self, key: str) -> DocExtraction | None: ...
    def put(self, key: str, value: DocExtraction) -> None: ...


class SqliteCache:
    def __init__(self, db: sqlite_utils.Database) -> None:
        self.table = db.table("extraction_cache")
        if not self.table.exists():
            self.table.create({"key": str, "value": str}, pk="key")

    def get(self, key: str) -> DocExtraction | None:
        rows = list(self.table.rows_where("key = ?", [key]))
        return DocExtraction.model_validate_json(rows[0]["value"]) if rows else None

    def put(self, key: str, value: DocExtraction) -> None:
        self.table.upsert({"key": key, "value": value.model_dump_json()}, pk="key")


class JsonlCache:
    """A committed 'cassette' so evals and CI replay recorded LLM output."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, str] = {}
        if path.exists():
            for line in path.read_text().splitlines():
                if line.strip():
                    row = json.loads(line)
                    self.data[row["key"]] = row["value"]

    def get(self, key: str) -> DocExtraction | None:
        v = self.data.get(key)
        return DocExtraction.model_validate_json(v) if v else None

    def put(self, key: str, value: DocExtraction) -> None:
        self.data[key] = value.model_dump_json()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w") as f:
            for k in sorted(self.data):
                f.write(json.dumps({"key": k, "value": self.data[k]}) + "\n")


# --- grounding -----------------------------------------------------------------------


def _amounts(r: Record) -> list[Decimal]:
    out = []
    for name in ("amount", "old_amount", "new_amount"):
        v = getattr(r, name, None)
        if v is not None:
            out.append(Decimal(v))
    return out


def amount_in_text(amount: Decimal, text: str) -> bool:
    plain = text.replace(",", "")
    forms = {f"{amount:.2f}", f"{amount.normalize():f}"}
    if amount == amount.to_integral():
        forms.add(str(int(amount)))
    return any(re.search(rf"(?<![\d.]){re.escape(f)}(?![\d])", plain) for f in forms)


def ground(ex: DocExtraction, text: str) -> tuple[DocExtraction, int]:
    """Drop records whose amounts don't appear in the source text."""
    dropped = 0
    kept: dict[str, list[Record]] = {}
    for field in ("transactions", "receipts", "renewals", "price_changes", "cancellations"):
        good = []
        for r in getattr(ex, field):
            if all(amount_in_text(a, text) for a in _amounts(r)):
                good.append(r)
            else:
                dropped += 1
        kept[field] = good
    return ex.model_copy(update=kept), dropped


# --- extractor ------------------------------------------------------------------------


class ExtractStats(BaseModel):
    documents: int = 0
    by_rules: int = 0
    cache_hits: int = 0
    llm_docs: int = 0
    llm_calls: int = 0
    failed_docs: int = 0
    dropped_ungrounded: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = Decimal(0)
    cost_inr: Decimal = Decimal(0)
    stopped: str | None = None  # e.g. budget exceeded; finished docs are still cached


def cache_key(model: str, text: str) -> str:
    return hashlib.sha256(f"{PROMPT_VERSION}\0{model}\0{text}".encode()).hexdigest()


class Extractor:
    def __init__(
        self,
        provider: LLMProvider | None,
        model: str,
        redactor: Redactor,
        cache: ExtractionCache,
        budget: Budget,
        *,
        batch_docs: int = 8,
        batch_chars: int = 16_000,
    ) -> None:
        # Behind the egress guard no matter what the caller passed in.
        self.provider = GuardedProvider(provider, EgressGuard(redactor)) if provider else None
        self.model = model
        self.cache = cache
        self.budget = budget
        self.batch_docs = batch_docs
        self.batch_chars = batch_chars

    def run(
        self, docs: list[Document], progress: Progress | None = None
    ) -> tuple[dict[str, DocExtraction], ExtractStats]:
        progress = progress or NullProgress()
        stats = ExtractStats(documents=len(docs))
        results: dict[str, DocExtraction] = {}
        pending: list[Document] = []

        progress.start("extract", "Extracting", len(docs))
        for doc in docs:
            if doc.source == "sms" and (ex := extract_sms(doc.id, doc.redacted_text)):
                results[doc.id] = ex
                stats.by_rules += 1
                progress.advance("extract", detail=f"rule · {doc.title}")
            elif cached := self.cache.get(cache_key(self.model, doc.redacted_text)):
                results[doc.id] = cached.model_copy(update={"evidence_id": doc.id})
                stats.cache_hits += 1
                progress.advance("extract", detail=f"cache · {doc.title}")
            else:
                pending.append(doc)

        if pending and self.provider is None:
            stats.failed_docs += len(pending)  # no key: report, don't guess
            progress.advance("extract", len(pending), detail="no LLM configured")
            pending = []

        batches = self._batches(pending)
        for i, batch in enumerate(batches):
            try:
                self._extract_batch(batch, results, stats, progress.advance)
            except BudgetExceeded as e:
                stats.stopped = e.reason
                stats.failed_docs += sum(len(b) for b in batches[i + 1 :])
                break
        progress.finish(
            "extract",
            f"{stats.llm_docs} via LLM, {stats.cache_hits} cached, {stats.by_rules} rules",
        )
        stats.cost_usd = self.budget.spent_usd
        stats.cost_inr = self.budget.to_inr(self.budget.spent_usd)
        stats.input_tokens, stats.output_tokens = (
            self.budget.input_tokens,
            self.budget.output_tokens,
        )
        return results, stats

    def _batches(self, docs: list[Document]) -> list[list[Document]]:
        batches: list[list[Document]] = []
        cur: list[Document] = []
        size = 0
        for d in docs:
            n = min(len(d.redacted_text), MAX_DOC_CHARS)
            if cur and (len(cur) >= self.batch_docs or size + n > self.batch_chars):
                batches.append(cur)
                cur, size = [], 0
            cur.append(d)
            size += n
        return [*batches, cur] if cur else batches

    def _extract_batch(
        self,
        batch: list[Document],
        results: dict[str, DocExtraction],
        stats: ExtractStats,
        advance: Callable[..., None],
    ) -> None:
        assert self.provider is not None
        prompt = "\n\n".join(
            f'<doc id="{d.id}" source="{d.source}" date="{d.ts.date() if d.ts else ""}">\n'
            f"{d.redacted_text[:MAX_DOC_CHARS]}\n</doc>"
            for d in batch
        )
        try:
            resp = self.provider.generate(
                model=self.model,
                system=SYSTEM,
                messages=[Message(role="user", text=prompt)],
                response_schema=BatchExtraction,
            )
        except LLMError:
            if len(batch) > 1:  # one bad doc shouldn't sink its neighbours: split
                mid = len(batch) // 2
                self._extract_batch(batch[:mid], results, stats, advance)
                self._extract_batch(batch[mid:], results, stats, advance)
                return
            stats.failed_docs += 1
            advance("extract", detail=f"failed · {batch[0].title}")
            return
        stats.llm_calls += 1

        by_id = {d.id: d for d in batch}
        got = {e.evidence_id: e for e in BatchExtraction.model_validate_json(resp.text).documents}
        for doc_id, doc in by_id.items():
            ex = got.get(doc_id, DocExtraction(evidence_id=doc_id, is_financial=False))
            ex, dropped = ground(ex, doc.redacted_text)
            stats.dropped_ungrounded += dropped
            results[doc_id] = ex
            self.cache.put(cache_key(self.model, doc.redacted_text), ex)
            stats.llm_docs += 1
            advance("extract", detail=f"llm · {doc.title}")
        # Charge last: if this call tips us over budget, its (already paid-for)
        # results are cached above rather than thrown away.
        self.budget.charge(resp.usage or Usage(), self.model)
