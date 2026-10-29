"""`leak extract`: documents -> extractions -> ledger."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from leak.config import Settings
from leak.extract.extractor import (
    ExtractionCache,
    Extractor,
    ExtractStats,
    JsonlCache,
    SqliteCache,
)
from leak.ingest.progress import Progress
from leak.ingest.scan import open_store
from leak.ledger.normalize import LedgerReport, build_ledger
from leak.llm.factory import make_provider
from leak.llm.provider import LLMProvider

_REPO_CASSETTES = Path(__file__).resolve().parents[2] / "evals" / "cassettes"
_PKG_CASSETTES = Path(__file__).resolve().parents[1] / "_cassettes"  # bundled in the wheel
CASSETTE_DIR = _REPO_CASSETTES if _REPO_CASSETTES.is_dir() else _PKG_CASSETTES


def cassette_path(model: str) -> Path:
    """`google/gemini-3.5-flash-lite` -> extract-google--gemini-3.5-flash-lite.jsonl"""
    return CASSETTE_DIR / f"extract-{model.replace('/', '--')}.jsonl"


def demo_cache(model: str) -> JsonlCache:
    """The demo replays the recorded eval cassette: works with no API key, costs ₹0."""
    return JsonlCache(cassette_path(model))


class ExtractRunReport(BaseModel):
    extract: ExtractStats
    ledger: LedgerReport


def run_extract(
    settings: Settings,
    *,
    provider: LLMProvider | None = None,
    cache: ExtractionCache | None = None,
    progress: Progress | None = None,
) -> ExtractRunReport:
    store, redactor = open_store(settings)
    if provider is None:
        provider = make_provider(settings)
    extractor = Extractor(
        provider,
        settings.llm.extract_model,
        redactor,
        cache or SqliteCache(store.db),
        settings.make_budget(),
    )
    # Statement pages are parsed deterministically; only emails + SMS need this.
    docs = [d for d in store.documents() if d.source in ("gmail", "sms")]
    results, stats = extractor.run(docs, progress)
    store.save_extractions(results)
    return ExtractRunReport(extract=stats, ledger=build_ledger(store))
