"""Local web API for the dashboard. Binds to 127.0.0.1 only.

A local server that can create Gmail drafts and unmask PII has to assume that
any website you visit may try to talk to it. So:

* Host header must be localhost/127.0.0.1 (blocks DNS-rebinding attacks).
* No CORS headers, ever: other origins can't read responses.
* Every state-changing request and every unmask needs the per-process
  `X-Leak-Token`. Custom headers force a CORS preflight, which we never
  approve, so a cross-site form or fetch can't forge one.
"""

from __future__ import annotations

import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from leak.actions.execute import ActionError, Executor
from leak.actions.queue import SqliteApprovalQueue
from leak.config import Settings
from leak.findings import SAVING_ACTIONS, Finding, potential_savings
from leak.harness.trace import list_runs, load_trace, trace_path
from leak.ingest.scan import open_store
from leak.privacy.redact import Redactor
from leak.report import recurring_spend, report_findings
from leak.share import CardPrivacyError, LeakCard, build_card, render_svg
from leak.store import Store

# `npm run build` in web/ writes the static dashboard into the package
# (leak/_web) so wheels ship it; nothing to build at runtime.
WEB_OUT = Path(__file__).resolve().parents[1] / "_web"
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


@dataclass
class _Ctx:
    store: Store
    redactor: Redactor
    executor: Executor


class ActionRequest(BaseModel):
    tool: str


def create_app(
    settings: Settings,
    *,
    demo: bool = False,
    token: str | None = None,
    gmail: Callable[[], Any] | None = None,  # tests inject a fake Gmail service
) -> FastAPI:
    app = FastAPI(title="Leak", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.token = token or secrets.token_urlsafe(24)
    # SQLite connections can't cross threads and FastAPI runs sync endpoints in a
    # threadpool, so each worker thread lazily opens its own store.
    local = threading.local()
    SqliteApprovalQueue(open_store(settings)[0])  # create tables once, before any threads

    def ctx() -> _Ctx:
        if not hasattr(local, "ctx"):
            store, redactor = open_store(settings)
            local.ctx = _Ctx(store, redactor, Executor(settings, store, demo=demo, gmail=gmail))
        c: _Ctx = local.ctx
        return c

    @app.middleware("http")
    async def local_only(request: Request, call_next: Any) -> Response:
        host = (request.headers.get("host") or "").rsplit(":", 1)[0]
        if host not in ALLOWED_HOSTS:
            return JSONResponse({"detail": "Leak only serves localhost"}, status_code=403)
        response: Response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def require_token(x_leak_token: str = Header(default="")) -> None:
        if not secrets.compare_digest(x_leak_token, app.state.token):
            raise HTTPException(403, "missing or bad X-Leak-Token")

    def finding_or_404(fid: str) -> Finding:
        f = ctx().store.get_finding(fid)
        if f is None:
            raise HTTPException(404, f"no finding {fid}")
        return f

    # -- session + summary ------------------------------------------------------------
    @app.get("/api/session")
    def session() -> dict[str, Any]:
        return {
            "token": app.state.token,
            "demo": demo,
            "models": {"extract": settings.llm.extract_model, "reason": settings.llm.reason_model},
        }

    @app.get("/api/summary")
    def summary() -> dict[str, Any]:
        fs = report_findings(ctx().store)
        review = sum(
            (
                Decimal(f.annual_impact_inr)
                for f in fs
                if f.suggested_action == "review" and f.type != "recurring"
            ),
            Decimal(0),
        )
        recurring = recurring_spend(ctx().store)
        return {
            "potential_savings_inr": float(potential_savings(fs)),
            "review_inr": float(review),
            "recurring_inr": float(recurring),
            "findings": len(fs),
            "actionable": sum(f.suggested_action in SAVING_ACTIONS for f in fs),
            "pending_actions": len(ctx().executor.queue.pending()),
            "ledger": ctx().store.counts(),
        }

    # -- the shareable Leak Card ------------------------------------------------------------
    def card_or_422(names: bool) -> LeakCard:
        try:
            return build_card(report_findings(ctx().store), names=names)
        except CardPrivacyError as e:
            raise HTTPException(422, str(e)) from e

    @app.get("/api/share-card")
    def share_card(names: bool = True) -> dict[str, Any]:
        card = card_or_422(names)
        return {
            "total_inr": float(card.total_inr),
            "kinds": card.kinds,
            "rows": [
                {"name": r.name, "label": r.label, "annual_inr": float(r.annual_inr)}
                for r in card.rows
            ],
            "more": card.more,
            "text": card.share_text(),
        }

    @app.get("/api/share-card.svg")
    def share_card_svg(names: bool = True) -> Response:
        return Response(
            render_svg(card_or_422(names)),
            media_type="image/svg+xml",
            # An SVG opened directly is a document: never let it run anything.
            headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"},
        )

    # -- findings + evidence -------------------------------------------------------------
    @app.get("/api/findings")
    def findings() -> list[dict[str, Any]]:
        pending = {(a.args.get("finding_id"), a.tool) for a in ctx().executor.queue.pending()}
        return [
            f.model_dump(mode="json") | {"pending": sorted(t for fid, t in pending if fid == f.id)}
            for f in report_findings(ctx().store)
        ]

    @app.get("/api/findings/{fid}")
    def finding(fid: str) -> dict[str, Any]:
        f = finding_or_404(fid)
        ev = [
            {
                "evidence_id": e.evidence_id,
                "note": e.note,
                "text": ctx().store.read_evidence(e.evidence_id),
            }
            for e in f.evidence
        ]
        return f.model_dump(mode="json") | {"evidence_detail": ev}

    @app.get("/api/evidence/{eid:path}/unmasked", dependencies=[Depends(require_token)])
    def evidence_unmasked(eid: str) -> dict[str, Any]:
        text = ctx().store.read_evidence(eid)
        if text is None:
            raise HTTPException(404, f"no evidence {eid}")
        return {"evidence_id": eid, "text": ctx().redactor.vault.unmask(text)}

    # -- actions ---------------------------------------------------------------------------
    @app.post("/api/findings/{fid}/actions", dependencies=[Depends(require_token)])
    def request_action(fid: str, body: ActionRequest) -> dict[str, Any]:
        try:
            return ctx().executor.request(fid, body.tool).model_dump(mode="json")
        except ActionError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/api/actions")
    def actions() -> list[dict[str, Any]]:
        out = []
        for a in ctx().executor.queue.all():
            row = a.model_dump(mode="json")
            f = ctx().store.get_finding(str(a.args.get("finding_id")))
            row["finding"] = f.model_dump(mode="json") if f else None
            if a.status in ("pending", "failed"):
                try:
                    row["preview"] = ctx().executor.preview(a)
                except (ActionError, ValueError) as e:
                    row["preview"] = {"kind": "error", "error": str(e)}
            out.append(row)
        return out

    @app.post("/api/actions/{aid}/approve", dependencies=[Depends(require_token)])
    def approve(aid: str) -> dict[str, Any]:
        try:
            return ctx().executor.approve(aid).model_dump(mode="json")
        except ActionError as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/api/actions/{aid}/reject", dependencies=[Depends(require_token)])
    def reject(aid: str) -> dict[str, Any]:
        try:
            return ctx().executor.reject(aid).model_dump(mode="json")
        except ActionError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/api/reminders/{fid}.ics")
    def reminder(fid: str) -> FileResponse:
        path = settings.paths.data_dir / "reminders" / f"{finding_or_404(fid).id}.ics"
        if not path.exists():
            raise HTTPException(404, "approve the reminder first")
        return FileResponse(path, media_type="text/calendar", filename=f"leak-{fid}.ics")

    # -- traces / what the LLM saw --------------------------------------------------------------
    @app.get("/api/runs")
    def runs() -> list[dict[str, Any]]:
        out = []
        for p in list_runs(settings.trace_dir)[:50]:
            ev = load_trace(p)
            start = next((e for e in ev if e.type == "run_start"), None)
            end = next((e for e in ev if e.type == "run_end"), None)
            out.append(
                {
                    "run_id": p.stem,
                    "started": start.ts.isoformat() if start else None,
                    "model": start.data.get("model") if start else None,
                    "prompt": (start.data.get("prompt") or "")[:160] if start else "",
                    "status": end.data["status"] if end else "running/crashed",
                    "budget": end.data["budget"] if end else None,
                }
            )
        return out

    def events_or_404(run_id: str) -> list[Any]:
        if not run_id.replace("_", "").isalnum():
            raise HTTPException(400, "bad run id")
        path = trace_path(settings.trace_dir, run_id)
        if not path.exists():
            raise HTTPException(404, f"no run {run_id}")
        return load_trace(path)

    @app.get("/api/runs/{run_id}")
    def run(run_id: str) -> list[dict[str, Any]]:
        return [e.model_dump(mode="json") for e in events_or_404(run_id)]

    @app.get("/api/runs/{run_id}/payloads")
    def payloads(run_id: str) -> list[dict[str, Any]]:
        """Exactly what was sent to the model each step (already redacted at egress)."""
        return [
            {"step": e.step, "ts": e.ts.isoformat(), **e.data}
            for e in events_or_404(run_id)
            if e.type == "llm_request"
        ]

    # -- the dashboard ---------------------------------------------------------------------------
    if WEB_OUT.is_dir():
        app.mount("/", StaticFiles(directory=WEB_OUT, html=True), name="web")
    else:

        @app.get("/", response_class=HTMLResponse)
        def no_ui() -> str:
            return (
                "<h1>Leak API is running</h1><p>Build the dashboard: <code>cd web && npm "
                "install && npm run build</code>, then restart <code>leak serve</code>.</p>"
            )

    return app
