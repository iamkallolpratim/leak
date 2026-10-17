"""The auditor agent's tools.

Every read tool returns *redacted* data only. `query_ledger` runs on a
separate read-only SQLite connection with an authorizer that allows SELECTs
on four derived tables and nothing else -- not the raw document text, not the
vault (a different file), no writes, no ATTACH, no PRAGMA.

Write tools (`draft_cancellation_email`, `create_renewal_reminder`) are
registered as `needs_approval`: the loop queues them and never calls them.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from leak.extract.extractor import amount_in_text
from leak.findings import Finding
from leak.harness.grounding import Grounder
from leak.harness.tools import ToolRegistry
from leak.ledger.fx import fx_rate as _fx_rate
from leak.ledger.merchants import default_catalog
from leak.privacy.guard import vet_outbound
from leak.store import Store

READABLE_TABLES = {"ledger", "events", "findings", "raw_transactions"}
MAX_ROWS = 100


# --- read-only SQL ------------------------------------------------------------------


def readonly_query(db_path: str, sql: str, max_rows: int = MAX_ROWS) -> list[dict[str, Any]]:
    body = sql.strip().rstrip(";").strip()
    if ";" in body:
        raise ValueError("one statement only")
    if not re.match(r"(?is)^(select|with)\b", body):
        raise ValueError("only SELECT / WITH queries are allowed")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    def authorizer(action: int, arg1: str | None, arg2: str | None, *_: Any) -> int:
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            return (
                sqlite3.SQLITE_OK
                if arg1 is None or arg1 in READABLE_TABLES
                else (sqlite3.SQLITE_DENY)
            )
        if action == sqlite3.SQLITE_FUNCTION:
            return (
                sqlite3.SQLITE_DENY
                if (arg2 or "").lower() == "load_extension"
                else (sqlite3.SQLITE_OK)
            )
        return sqlite3.SQLITE_DENY  # writes, ATTACH, PRAGMA, everything else

    conn.set_authorizer(authorizer)
    try:
        cur = conn.execute(body)
        rows = cur.fetchmany(max_rows + 1)
    except sqlite3.DatabaseError as e:
        raise ValueError(f"query failed: {e}. Readable tables: {sorted(READABLE_TABLES)}") from e
    finally:
        conn.close()
    out = [dict(r) for r in rows[:max_rows]]
    if len(rows) > max_rows:
        out.append({"_truncated": f"more than {max_rows} rows; add LIMIT or aggregate"})
    return out


# --- local mail search ----------------------------------------------------------------

_TOKEN = re.compile(r'(\w+):("[^"]+"|\S+)|"([^"]+)"|(\S+)')


def search_documents(store: Store, query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Gmail-style subset over locally synced mail + SMS: words, "phrases",
    from:, subject:, after:YYYY-MM-DD, before:YYYY-MM-DD."""
    terms: list[str] = []
    filters: dict[str, str] = {}
    for key, val, phrase, word in _TOKEN.findall(query):
        if key:
            filters[key.lower()] = val.strip('"').lower()
        else:
            terms.append((phrase or word).lower())
    hits = []
    for d in store.documents():
        if d.source not in ("gmail", "sms"):
            continue
        text = d.redacted_text.lower()
        day = d.ts.date().isoformat() if d.ts else ""
        if "from" in filters and filters["from"] not in d.sender.lower():
            continue
        if "subject" in filters and filters["subject"] not in d.title.lower():
            continue
        if "after" in filters and not day > filters["after"]:
            continue
        if "before" in filters and not (day and day < filters["before"]):
            continue
        if not all(t in text for t in terms):
            continue
        pos = text.find(terms[0]) if terms else 0
        snippet = d.redacted_text[max(0, pos - 80) : pos + 160].replace("\n", " ")
        hits.append(
            {
                "evidence_id": d.id,
                "source": d.source,
                "date": day,
                "subject": d.title,
                "from": d.sender,
                "snippet": snippet,
            }
        )
        if len(hits) >= limit:
            break
    return hits


# --- tool registry ----------------------------------------------------------------------


class SqlArgs(BaseModel):
    sql: str = Field(description="One SELECT over ledger / events / findings / raw_transactions")


class IdArgs(BaseModel):
    id: str


class EvidenceArgs(BaseModel):
    evidence_id: str


class SearchArgs(BaseModel):
    query: str = Field(description="e.g. 'from:netflix after:2026-06-01 \"price\"'")


class MerchantArgs(BaseModel):
    name: str = Field(description="Merchant name or catalog id, e.g. 'Hotstar' or 'netflix'")


class FxArgs(BaseModel):
    currency: str
    on: date


class AskArgs(BaseModel):
    question: str = Field(description="One short, specific question for the user")


class SubmitArgs(BaseModel):
    finding: Finding = Field(description="id and source are assigned by the system")
    based_on: list[str] = Field(
        default_factory=list, description="Detector finding ids this confirms or replaces"
    )


class DismissArgs(BaseModel):
    finding_id: str
    reason: str


class ActionArgs(BaseModel):
    finding_id: str


class NoArgs(BaseModel):
    pass


def finding_grounder(store: Store) -> Grounder[Finding]:
    def amount_is_cited(f: Finding) -> str | None:
        if f.type == "forex":  # a computed average of fee lines; lines are checked to exist
            return None
        texts = [store.read_evidence(e.evidence_id, context_lines=0) or "" for e in f.evidence]
        if any(amount_in_text(Decimal(f.amount_inr), t) for t in texts):
            return None
        return f"amount_inr {f.amount_inr} does not appear in any cited evidence"

    def annual_is_sane(f: Finding) -> str | None:
        if Decimal(f.annual_impact_inr) < 0:
            return "annual_impact_inr is negative"
        if Decimal(f.annual_impact_inr) > Decimal(f.amount_inr) * 52 + 1:
            return "annual_impact_inr exceeds 52x the per-charge amount"
        return None

    def no_personal_data(f: Finding) -> str | None:
        # Model-written text is stored, shown, and later used in drafts and
        # reminders: it must not smuggle identifiers or links in.
        notes = {f"evidence note {i}": e.note for i, e in enumerate(f.evidence) if e.note}
        problems = vet_outbound(
            notes, untrusted={"merchant": f.merchant, "explanation": f.explanation}
        )
        if problems:
            return "remove personal data / links and resubmit: " + "; ".join(problems)
        return None

    return Grounder(
        evidence_of=lambda f: [e.evidence_id for e in f.evidence],
        exists=store.evidence_exists,
        rules=[amount_is_cited, annual_is_sane, no_personal_data],
    )


def build_registry(
    store: Store,
    *,
    ask_user: Callable[[str], str] | None = None,
    on_submit: Callable[[Finding], None] | None = None,
) -> ToolRegistry:
    reg = ToolRegistry()
    grounder = finding_grounder(store)
    catalog = default_catalog()
    db_path = str(store.path)

    @reg.tool(
        description="Run one read-only SQL SELECT. Tables: ledger(id, txn_date, merchant_id, "
        "merchant, category, description, amount_inr, currency, original_amount, direction, "
        "account, channel, sources, evidence, related_to), events(id, kind, merchant_id, "
        "merchant, event_date, amount, old_amount, new_amount, currency, cadence, is_trial, "
        "plan, next_date, evidence_id), findings(id, source, status, annual_impact_inr, data)."
    )
    def query_ledger(args: SqlArgs) -> list[dict[str, Any]]:
        return readonly_query(db_path, args.sql)

    @reg.tool(description="List active findings (detector + yours), highest annual impact first.")
    def list_findings(args: NoArgs) -> list[dict[str, Any]]:
        return [
            {
                "id": f.id,
                "type": f.type,
                "merchant": f.merchant,
                "amount_inr": str(f.amount_inr),
                "cadence": f.cadence,
                "annual_impact_inr": str(f.annual_impact_inr),
                "action": f.suggested_action,
                "confidence": f.confidence,
                "source": f.source,
                "account": f.account,
                "n_evidence": len(f.evidence),
            }
            for f, _, _ in store.findings()
        ]

    @reg.tool(description="Full detail of one finding, including its evidence ids.")
    def get_finding(args: IdArgs) -> dict[str, Any]:
        f = store.get_finding(args.id)
        if f is None:
            raise ValueError(f"no finding {args.id}")
        return f.model_dump(mode="json")

    @reg.tool(
        description="Search locally synced emails + SMS (redacted). Returns evidence ids "
        "and snippets."
    )
    def search_gmail(args: SearchArgs) -> list[dict[str, Any]]:
        return search_documents(store, args.query)

    @reg.tool(
        description="Read the redacted source behind an evidence id (statement line with "
        "context, or an email/SMS)."
    )
    def read_evidence(args: EvidenceArgs) -> str:
        text = store.read_evidence(args.evidence_id)
        if text is None:
            raise ValueError(f"evidence {args.evidence_id} does not exist")
        return text

    @reg.tool(description="Catalog info: category, typical price, cancel URL/email, bundles.")
    def get_merchant_info(args: MerchantArgs) -> dict[str, Any]:
        m = catalog.get(args.name) or catalog.resolve(args.name)
        if m is None:
            return {"found": False}
        return {"found": True, **m.model_dump(exclude={"aliases", "domains"})}

    @reg.tool(description="Approximate INR rate for a currency on a date (offline table).")
    def fx_rate(args: FxArgs) -> dict[str, Any]:
        return _fx_rate(args.currency, args.on).model_dump(mode="json")

    @reg.tool(
        name="ask_user",
        description="Ask the user a clarifying question about an "
        "ambiguous charge. Use sparingly; the run pauses until they answer.",
    )
    def ask_user_tool(args: AskArgs) -> str:
        if ask_user is None:
            return (
                "The user is not available. Proceed with your best judgment and lower confidence."
            )
        return ask_user(args.question)

    @reg.tool(
        description="Submit a final, evidence-backed finding. It is validated: every "
        "evidence id must exist and amount_inr must appear in the cited evidence. "
        "Rejections explain what to fix."
    )
    def submit_finding(args: SubmitArgs) -> dict[str, Any]:
        f = args.finding.model_copy(update={"source": "agent"})
        if not f.merchant_id and (m := catalog.resolve(f.merchant)):
            f = f.model_copy(update={"merchant_id": m.id})  # keeps savings dedupe + drafts exact
        result = grounder.check(f)
        if not result.ok:
            raise ValueError("finding rejected: " + "; ".join(result.errors))
        f = f.with_id(f.account or "", str(f.amount_inr))
        store.save_findings([f])
        for old in args.based_on:
            if old != f.id:
                store.set_finding_status(old, "superseded", f"replaced by {f.id}")
        if on_submit:
            on_submit(f)
        return {"accepted": f.id}

    @reg.tool(
        description="Dismiss a detector finding that is not a real leak (e.g. a bill the "
        "user clearly needs). Give a reason."
    )
    def dismiss_finding(args: DismissArgs) -> dict[str, Any]:
        if not store.set_finding_status(args.finding_id, "dismissed", args.reason):
            raise ValueError(f"no finding {args.finding_id}")
        return {"dismissed": args.finding_id}

    @reg.tool(
        description="Create a Gmail DRAFT asking the merchant to cancel. Requires the "
        "user's approval; it is queued, never sent.",
        permission="needs_approval",
    )
    def draft_cancellation_email(args: ActionArgs) -> str:
        raise NotImplementedError("executed from the approval queue (Phase 5)")

    @reg.tool(
        description="Create a calendar reminder (.ics) before the next renewal. Requires "
        "the user's approval.",
        permission="needs_approval",
    )
    def create_renewal_reminder(args: ActionArgs) -> str:
        raise NotImplementedError("executed from the approval queue (Phase 5)")

    return reg
