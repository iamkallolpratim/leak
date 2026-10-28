"""Wires the harness, the auditor tools and the Pro model into one run.

detectors ──> findings table ──> AgentLoop(Pro, tools, budget, egress guard) ──> report
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from leak.actions.queue import SqliteApprovalQueue
from leak.agent.prompts import AUDITOR_SYSTEM, auditor_task
from leak.agent.tools import build_registry
from leak.config import Settings
from leak.detect import run_detectors
from leak.findings import Finding
from leak.harness import AgentLoop, Budget, PermissionPolicy, RunResult
from leak.ingest.scan import open_store
from leak.llm.factory import make_provider
from leak.llm.provider import LLMProvider
from leak.privacy.guard import EgressGuard
from leak.store import Store


@dataclass
class AuditResult:
    detector_findings: int
    agent: RunResult | None = None


def detect_only(store: Store, today: date | None = None) -> list[Finding]:
    findings = run_detectors(store, today)
    store.save_findings(findings, replace_source="detector")
    return findings


def run_audit(
    settings: Settings,
    *,
    provider: LLMProvider | None = None,
    use_agent: bool = True,
    ask_user: Callable[[str], str] | None = None,
    today: date | None = None,
    budget: Budget | None = None,
) -> AuditResult:
    store, redactor = open_store(settings)
    # Fresh audit: drop the previous agent's findings, re-run detectors.
    store.save_findings([], replace_source="agent")
    findings = detect_only(store, today)
    result = AuditResult(detector_findings=len(findings))
    if not use_agent:
        return result
    if provider is None:
        provider = make_provider(settings)
        if provider is None:
            return result  # no LLM configured: detector-only report

    guard = EgressGuard(redactor)
    a = settings.agent
    loop = AgentLoop(
        provider=provider,
        model=settings.llm.reason_model,
        registry=build_registry(store, ask_user=ask_user),
        budget=budget
        or Budget(
            max_steps=a.max_steps,
            max_usd=a.max_usd,
            prices=settings.llm.prices,
            usd_inr=settings.budget.usd_inr,
        ),
        trace_dir=settings.trace_dir,
        system=AUDITOR_SYSTEM,
        policy=PermissionPolicy(settings.permissions),
        queue=SqliteApprovalQueue(store),  # survives the run: the UI approves later
        egress=guard.filter,
        trace_scrub=guard.scrub,
    )
    ledger = store.ledger()
    stats = {
        "entries": len(ledger),
        "from": ledger[0]["txn_date"] if ledger else None,
        "to": ledger[-1]["txn_date"] if ledger else None,
        "accounts": sorted({r["account"] for r in ledger if r["account"]}),
    }
    today = today or (date.fromisoformat(stats["to"]) if stats["to"] else date.today())  # type: ignore[arg-type]
    result.agent = loop.run(auditor_task(findings, stats, today))
    return result
