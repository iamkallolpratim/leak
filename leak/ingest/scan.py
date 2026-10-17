"""`leak scan`: run every configured ingester into the local store."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from leak.config import Settings
from leak.ingest.gmail import GmailSync, GmailSyncStats, ingest_eml_dir
from leak.ingest.progress import NullProgress, Progress
from leak.ingest.sms import SmsStats, ingest_sms_backup
from leak.ingest.statements import (
    LLMStatementParser,
    StatementError,
    StatementResult,
    ingest_statement,
)
from leak.privacy.redact import Redactor, Vault
from leak.store import Store

_REPO_DEMO = Path(__file__).resolve().parents[2] / "demo" / "sample_data"
_PKG_DEMO = Path(__file__).resolve().parents[1] / "_demo_data"  # bundled in the wheel
DEMO_DIR = _REPO_DEMO if _REPO_DEMO.is_dir() else _PKG_DEMO


class ScanReport(BaseModel):
    emails: int = 0
    gmail: GmailSyncStats | None = None
    statements: list[StatementResult] = Field(default_factory=list)
    statement_errors: list[str] = Field(default_factory=list)
    sms: list[SmsStats] = Field(default_factory=list)
    store_counts: dict[str, Any] = Field(default_factory=dict)
    pii_tokens: dict[str, int] = Field(default_factory=dict)


def open_store(settings: Settings) -> tuple[Store, Redactor]:
    vault = Vault(settings.vault_path)
    return Store(settings.db_path), Redactor(vault, settings.privacy.profile())


def run_scan(
    settings: Settings,
    *,
    eml_dir: Path | None = None,
    statement_paths: list[Path] | None = None,
    sms_paths: list[Path] | None = None,
    gmail_service: Any | None = None,
    email_limit: int | None = None,
    progress: Progress | None = None,
) -> ScanReport:
    """`email_limit` = only the newest N emails (Gmail and .eml folders)."""
    progress = progress or NullProgress()
    store, redactor = open_store(settings)
    report = ScanReport()

    if eml_dir is not None:
        report.emails = ingest_eml_dir(
            eml_dir, store=store, redactor=redactor, limit=email_limit, progress=progress
        )
    if gmail_service is not None:
        g = settings.gmail
        report.gmail = GmailSync(
            gmail_service,
            store=store,
            redactor=redactor,
            query=g.query,
            max_messages=g.max_messages,
            limit=email_limit,
            progress=progress,
        ).sync()

    fallback = None
    if settings.statements.llm_fallback and settings.gemini_api_key:
        from leak.llm.gemini import GeminiProvider

        fallback = LLMStatementParser(
            GeminiProvider(settings.gemini_api_key),
            settings.llm.extract_model,
            redactor,
            budget=settings.make_budget(),
        )
    paths = statement_paths or []
    if paths:
        progress.start("statements", "Parsing statements", len(paths))
    for path in paths:
        try:
            r = ingest_statement(
                path,
                store=store,
                redactor=redactor,
                passwords=settings.statements.all_passwords(),
                fallback=fallback,
            )
            report.statements.append(r)
            progress.advance("statements", detail=f"{path.name} · {r.parser}")
        except StatementError as e:
            report.statement_errors.append(str(e))
            progress.advance("statements", detail=f"{path.name} · skipped")
    if paths:
        progress.finish("statements", f"{len(report.statements)} parsed")

    for path in sms_paths or []:
        report.sms.append(
            ingest_sms_backup(path, store=store, redactor=redactor, progress=progress)
        )

    report.store_counts = store.counts()
    report.pii_tokens = redactor.vault.counts()
    return report


def demo_inputs(demo_dir: Path = DEMO_DIR) -> dict[str, Any]:
    return {
        "eml_dir": demo_dir / "emails",
        "statement_paths": sorted((demo_dir / "statements").glob("*.pdf")),
        "sms_paths": sorted((demo_dir / "sms").glob("*.xml")),
    }
