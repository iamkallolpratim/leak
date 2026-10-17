"""Local SQLite store. One file, gitignored, never leaves the machine."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import sqlite_utils

from leak.extract.schemas import DocExtraction
from leak.findings import Finding
from leak.ingest.models import Document, RawTransaction


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists()
        # Busy timeout + WAL: the web UI reads from several threads while the CLI
        # may be writing; wait for locks instead of failing.
        conn = sqlite3.connect(self.path, timeout=15)
        conn.execute("PRAGMA journal_mode=WAL")
        self.db = sqlite_utils.Database(conn)
        self._init()
        if new:
            os.chmod(self.path, 0o600)

    def _init(self) -> None:
        names = set(self.db.table_names())
        if "documents" not in names:
            self.db.table("documents").create(
                {"id": str, "source": str, "title": str, "sender": str, "ts": str,
                 "raw_text": str, "redacted_text": str, "content_hash": str, "meta": str},
                pk="id",
            )  # fmt: skip
            self.db.table("documents").create_index(["content_hash"])
        if "raw_transactions" not in names:
            self.db.table("raw_transactions").create(
                {"evidence_id": str, "document_id": str, "bank": str, "parser": str,
                 "account": str, "txn_date": str, "description": str, "amount": str,
                 "direction": str, "currency": str, "foreign_amount": str,
                 "foreign_currency": str, "balance": str},
                pk="evidence_id",
            )  # fmt: skip
        if "sync_state" not in names:
            self.db.table("sync_state").create({"source": str, "cursor": str}, pk="source")

    # -- documents -------------------------------------------------------------
    def has_document(self, doc_id: str) -> bool:
        return bool(self.db.execute("select 1 from documents where id = ?", [doc_id]).fetchone())

    def upsert_document(self, doc: Document) -> None:
        row = doc.model_dump(mode="json")
        row["meta"] = json.dumps(row["meta"])
        self.db.table("documents").upsert(row, pk="id")

    def get_document(self, doc_id: str) -> Document | None:
        rows = list(self.db.query("select * from documents where id = ?", [doc_id]))
        if not rows:
            return None
        row = dict(rows[0])
        row["meta"] = json.loads(row["meta"] or "{}")
        return Document.model_validate(row)

    def documents(self, source: str | None = None) -> list[Document]:
        sql, args = "select id from documents", []
        if source:
            sql, args = sql + " where source = ?", [source]
        ids = [r["id"] for r in self.db.query(sql + " order by ts, id", args)]
        return [d for i in ids if (d := self.get_document(i))]

    # -- transactions -----------------------------------------------------------
    def upsert_transactions(self, txns: Iterable[RawTransaction]) -> int:
        rows = [{k: (None if v is None else str(v)) for k, v in t.model_dump().items()}
                for t in txns]  # fmt: skip
        if rows:
            self.db.table("raw_transactions").upsert_all(rows, pk="evidence_id")
        return len(rows)

    def raw_transactions(self) -> list[RawTransaction]:
        rows = self.db.query("select * from raw_transactions order by txn_date, evidence_id")
        return [RawTransaction.model_validate(r) for r in rows]

    # -- extractions ------------------------------------------------------------------
    def save_extractions(self, results: dict[str, DocExtraction]) -> None:
        rows = [{"doc_id": k, "data": v.model_dump_json()} for k, v in results.items()]
        if rows:
            self.db.table("extractions").upsert_all(rows, pk="doc_id")

    def extractions(self) -> dict[str, DocExtraction]:
        if not self.db.table("extractions").exists():
            return {}
        return {
            r["doc_id"]: DocExtraction.model_validate_json(r["data"])
            for r in self.db.query("select doc_id, data from extractions")
        }

    # -- ledger --------------------------------------------------------------------------
    def replace_ledger(self, entries: list[dict[str, Any]], events: list[dict[str, Any]]) -> None:
        """Ledger is derived data: rebuilt from scratch each time."""
        with self.db.conn:
            for name, rows in (("ledger", entries), ("events", events)):
                table = self.db.table(name)
                if table.exists():
                    table.drop()
                if rows:
                    table.insert_all(rows, pk="id", alter=True)

    def ledger(self) -> list[dict[str, Any]]:
        if not self.db.table("ledger").exists():
            return []
        return list(self.db.query("select * from ledger order by txn_date, id"))

    def events(self) -> list[dict[str, Any]]:
        if not self.db.table("events").exists():
            return []
        return list(self.db.query("select * from events order by event_date, id"))

    # -- findings ----------------------------------------------------------------------
    def save_findings(self, findings: list[Finding], *, replace_source: str | None = None) -> None:
        t = self.db.table("findings")
        if replace_source and t.exists():
            t.delete_where("source = ?", [replace_source])
        rows = [{"id": f.id, "source": f.source, "status": "active",
                 "annual_impact_inr": float(f.annual_impact_inr), "note": None,
                 "data": f.model_dump_json()} for f in findings]  # fmt: skip
        if rows:
            t.upsert_all(rows, pk="id", alter=True)

    def set_finding_status(self, finding_id: str, status: str, note: str | None = None) -> bool:
        t = self.db.table("findings")
        if not t.exists() or not list(t.rows_where("id = ?", [finding_id])):
            return False
        t.update(finding_id, {"status": status, "note": note})
        return True

    def findings(self, status: str | None = "active") -> list[tuple[Finding, str, str | None]]:
        """(finding, status, note), highest annual impact first."""
        t = self.db.table("findings")
        if not t.exists():
            return []
        where, args = ("status = ?", [status]) if status else (None, [])
        rows = t.rows_where(where, args, order_by="annual_impact_inr desc")
        return [(Finding.model_validate_json(r["data"]), r["status"], r["note"]) for r in rows]

    def get_finding(self, finding_id: str) -> Finding | None:
        t = self.db.table("findings")
        rows = list(t.rows_where("id = ?", [finding_id])) if t.exists() else []
        return Finding.model_validate_json(rows[0]["data"]) if rows else None

    # -- evidence ------------------------------------------------------------------------
    def _split_line_id(self, evidence_id: str) -> tuple[str, int | None]:
        if evidence_id.startswith("pdf:") and ":l" in evidence_id:
            doc_id, line = evidence_id.rsplit(":l", 1)
            if line.isdigit():
                return doc_id, int(line)
        return evidence_id, None

    def evidence_exists(self, evidence_id: str) -> bool:
        doc_id, line = self._split_line_id(evidence_id)
        doc = self.get_document(doc_id)
        if doc is None:
            return False
        return line is None or 1 <= line <= len(doc.redacted_text.splitlines())

    def read_evidence(self, evidence_id: str, context_lines: int = 2) -> str | None:
        """Redacted excerpt: the cited line (+context) of a statement page, or the
        start of an email/SMS. Never the raw text."""
        doc_id, line = self._split_line_id(evidence_id)
        doc = self.get_document(doc_id)
        if doc is None:
            return None
        if line is None:
            return f"[{doc.source}] {doc.title}\n{doc.redacted_text[:1500]}"
        lines = doc.redacted_text.splitlines()
        if not 1 <= line <= len(lines):
            return None
        lo, hi = max(0, line - 1 - context_lines), min(len(lines), line + context_lines)
        body = "\n".join(("> " if i == line - 1 else "  ") + lines[i] for i in range(lo, hi))
        return f"[statement] {doc.title}, line {line}\n{body}"

    # -- sync cursors -------------------------------------------------------------
    def get_cursor(self, source: str) -> str | None:
        rows = list(self.db.query("select cursor from sync_state where source = ?", [source]))
        return str(rows[0]["cursor"]) if rows else None

    def set_cursor(self, source: str, cursor: str) -> None:
        self.db.table("sync_state").upsert({"source": source, "cursor": cursor}, pk="source")

    def counts(self) -> dict[str, Any]:
        by_source = {r["source"]: r["n"] for r in self.db.query(
            "select source, count(*) n from documents group by source")}  # fmt: skip
        n_txn = self.db.execute("select count(*) from raw_transactions").fetchone()[0]
        return {"documents": by_source, "raw_transactions": n_txn}
