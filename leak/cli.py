"""Leak CLI.

Commands:
    leak scan [--demo] [--gmail] [--statements DIR] [--sms FILE] [--limit N]
    leak gmail auth                one-time OAuth with your own GCP client
    leak demo-harness [--live]     run the harness with toy tools
    leak trace list                list recorded runs
    leak trace replay <run_id>     print a run step by step
    leak trace rerun <run_id>      re-execute the loop from recorded responses
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)
from rich.progress import Progress as RichBar
from rich.table import Table

from leak.actions.execute import Executor
from leak.config import Settings, load_settings
from leak.demo_tools import build_toy_registry
from leak.harness import AgentLoop, ReplayProvider, load_trace, render_replay
from leak.harness.permissions import PermissionPolicy
from leak.harness.trace import list_runs, trace_path
from leak.llm.provider import LLMProvider, LLMResponse, ToolCall, Usage

app = typer.Typer(no_args_is_help=True, help="Leak: find the money leaking out of your accounts.")
trace_app = typer.Typer(no_args_is_help=True, help="Inspect and replay run traces.")
app.add_typer(trace_app, name="trace")
gmail_app = typer.Typer(no_args_is_help=True, help="Gmail connection.")
app.add_typer(gmail_app, name="gmail")
console = Console()

DEMO_SYSTEM = (
    "You are a test agent for the Leak harness. Use the tools to answer. "
    "Some tools need user approval; if a call is queued, say so plainly."
)
DEMO_PROMPT = (
    "What is 2 + 3, what's the weather in Bengaluru, and draft a greeting "
    "email to friend@example.com."
)


def _scripted_demo(model: str) -> LLMProvider:
    from leak.llm.fake import ScriptedProvider

    u = Usage(input_tokens=1200, output_tokens=150)
    return ScriptedProvider([
        LLMResponse(model=model, usage=u, text="I'll compute the sum and check the weather.",
                    tool_calls=[ToolCall(id="c1", name="add", args={"a": 2, "b": 3}),
                                ToolCall(id="c2", name="get_weather", args={"city": "Bengaluru"})]),
        LLMResponse(model=model, usage=u, tool_calls=[
            ToolCall(id="c3", name="send_greeting_draft",
                     args={"to": "friend@example.com", "body": "Hi!"})]),
        LLMResponse(model=model, usage=u,
                    text="2 + 3 = 5. Bengaluru: 29°C, humid. The greeting draft is queued "
                         "for your approval and has not been created yet."),
    ])  # fmt: skip


@app.command("demo-harness")
def demo_harness(
    live: bool = typer.Option(False, help="Use real Gemini (needs GEMINI_API_KEY)."),
    max_steps: int | None = typer.Option(None, help="Override step budget."),
) -> None:
    """Run the harness end to end with toy tools."""
    s = load_settings()
    model = s.llm.reason_model
    if live:
        from leak.llm.gemini import GeminiProvider

        provider: LLMProvider = GeminiProvider(s.gemini_api_key)
    else:
        provider = _scripted_demo(model)
    budget = s.make_budget()
    if max_steps is not None:
        budget.max_steps = max_steps
    loop = AgentLoop(
        provider=provider,
        model=model,
        registry=build_toy_registry(),
        budget=budget,
        trace_dir=s.trace_dir,
        system=DEMO_SYSTEM,
        policy=PermissionPolicy(s.permissions),
    )
    result = loop.run(DEMO_PROMPT)
    typer.echo(f"status:  {result.status}" + (f" ({result.reason})" if result.reason else ""))
    typer.echo(f"answer:  {result.final_text}")
    for a in result.pending_actions:
        typer.echo(f"pending: {a.id} {a.tool}({a.args})")
    typer.echo(f"run_id:  {result.run_id}\n\nreplay with:  leak trace replay {result.run_id}")


class RichProgress:
    """Terminal animation for leak.ingest.progress.Progress (spinner + bar per task)."""

    def __init__(self, bar: RichBar) -> None:
        self.bar = bar
        self.tasks: dict[str, TaskID] = {}

    def start(self, key: str, label: str, total: int | None) -> None:
        self.tasks[key] = self.bar.add_task(label, total=total, detail="")

    def advance(self, key: str, n: int = 1, detail: str = "") -> None:
        detail = detail if len(detail) <= 50 else detail[:49] + "…"
        self.bar.update(self.tasks[key], advance=n, detail=detail)

    def finish(self, key: str, summary: str = "") -> None:
        task = self.tasks[key]
        done = self.bar.tasks[task].completed
        self.bar.update(task, total=done, completed=done, detail=summary)


def _progress_bar() -> RichBar:
    return RichBar(
        SpinnerColumn(finished_text="[green]✓[/green]"),
        TextColumn("{task.description:<20}"),
        BarColumn(bar_width=28),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TextColumn("[dim]{task.fields[detail]}"),
        console=console,
    )


@app.command()
def scan(
    demo: bool = typer.Option(False, help="Ingest the synthetic demo dataset."),
    gmail: bool = typer.Option(False, help="Sync Gmail (run `leak gmail auth` first)."),
    statements: Path | None = typer.Option(None, help="PDF file or folder of statements."),
    sms: list[Path] = typer.Option([], help="SMS Backup & Restore XML file(s)."),
    emails: Path | None = typer.Option(None, help="Folder of .eml files."),
    limit: int | None = typer.Option(
        None, "--limit", "-n", min=1, help="Only the newest N matching emails (a sample)."
    ),
) -> None:
    """Ingest emails, statements and SMS into the local store (redacted)."""
    from leak.ingest.scan import DEMO_DIR, demo_inputs, run_scan

    s = load_settings(overlay=DEMO_DIR / "demo.toml" if demo else None)
    inputs: dict[str, object] = demo_inputs() if demo else {}
    if not demo:
        stmt = statements or s.statements.dir
        if stmt:
            stmt = stmt.expanduser()
            inputs["statement_paths"] = sorted(stmt.glob("*.pdf")) if stmt.is_dir() else [stmt]
        inputs["sms_paths"] = [p.expanduser() for p in (sms or s.sms.paths)]
        inputs["eml_dir"] = emails
        if gmail:
            from leak.ingest.gmail import build_service, get_credentials

            creds = get_credentials(s.gmail.client_secret.expanduser(), s.gmail_token_path,
                                    enable_actions=s.gmail.enable_actions)  # fmt: skip
            inputs["gmail_service"] = build_service(creds)
    with _progress_bar() as bar:
        report = run_scan(s, **inputs, email_limit=limit, progress=RichProgress(bar))  # type: ignore[arg-type]

    t = Table(title=f"Scan → {s.db_path}")
    for col in ("source", "item", "result"):
        t.add_column(col)
    if report.emails:
        t.add_row("email", "eml files", f"{report.emails} emails")
    if report.gmail:
        g = report.gmail
        result = f"{g.fetched} new, {g.skipped_known} known"
        if g.errors:
            result += f" · [red]{g.errors} failed[/red] ({g.first_error})"
        t.add_row("gmail", "sync", result)
    for r in report.statements:
        t.add_row(
            "statement", r.file, f"{r.bank} · {r.parser} · {r.pages}p · {r.transactions} txns"
        )
    for err in report.statement_errors:
        t.add_row("statement", "[red]skipped[/red]", err)
    for m in report.sms:
        t.add_row(
            "sms",
            m.file,
            f"{m.kept} kept · {m.skipped_personal} personal · "
            f"{m.skipped_otp} OTP · {m.skipped_other} other (dropped)",
        )
    console.print(t)
    pii = ", ".join(f"{k} {v}" for k, v in sorted(report.pii_tokens.items()))
    console.print(f"stored: {report.store_counts}")
    console.print(f"masked PII (unique values in local vault): {pii or 'none'}")
    if report.gmail and not report.gmail.cursor_updated:
        why = "limited scan" if report.gmail.limited else "some messages failed"
        console.print(f"[yellow]{why}: sync cursor not updated; next scan re-checks this range[/]")


@app.command()
def extract(demo: bool = typer.Option(False, help="Use the demo database.")) -> None:
    """Extract receipts/renewals/charges from stored emails + SMS, then rebuild the ledger."""
    from leak.extract.run import run_extract
    from leak.ingest.scan import DEMO_DIR

    s = load_settings(overlay=DEMO_DIR / "demo.toml" if demo else None)
    from leak.extract.run import demo_cache

    cache = demo_cache(s.llm.extract_model) if demo else None
    with _progress_bar() as bar:
        r = run_extract(s, cache=cache, progress=RichProgress(bar))
    x, lg = r.extract, r.ledger
    console.print(
        f"extract: {x.documents} docs · {x.by_rules} by SMS rules · {x.cache_hits} cached · "
        f"{x.llm_docs} via LLM in {x.llm_calls} calls · {x.dropped_ungrounded} ungrounded dropped"
    )
    if x.failed_docs:
        console.print(f"[yellow]{x.failed_docs} docs not extracted[/] {x.stopped or ''}".rstrip())
    console.print(
        f"cost: {x.input_tokens} in / {x.output_tokens} out tokens · "
        f"${x.cost_usd:.4f} (₹{x.cost_inr:.2f})"
    )
    console.print(
        f"ledger: {lg.entries} entries ({lg.multi_source} confirmed by >1 source) · "
        f"{lg.events} events · {lg.unresolved_merchants} unresolved merchants · {lg.by_source}"
    )


@app.command()
def ledger(
    demo: bool = typer.Option(False, help="Use the demo database."),
    merchant: str | None = typer.Option(None, help="Filter by merchant id, e.g. netflix."),
    limit: int = typer.Option(40, "--limit", "-n", min=1, help="Rows to show."),
) -> None:
    """Show the normalized ledger (masked; nothing here leaves your machine)."""
    from leak.ingest.scan import DEMO_DIR, open_store

    s = load_settings(overlay=DEMO_DIR / "demo.toml" if demo else None)
    store, _ = open_store(s)
    rows = [r for r in store.ledger() if not merchant or r["merchant_id"] == merchant]
    t = Table(title=f"Ledger ({len(rows)} entries)")
    for col in ("date", "merchant", "amount ₹", "orig", "acct", "via", "sources"):
        t.add_column(col, justify="right" if col.startswith("amount") else "left")
    for r in rows[-limit:]:
        sign = "+" if r["direction"] == "credit" else ""
        orig = "" if r["currency"] == "INR" else f"{r['currency']} {r['original_amount']:.2f}"
        t.add_row(r["txn_date"], r["merchant"], f"{sign}{r['amount_inr']:,.2f}", orig,
                  r["account"] or "", r["channel"], r["sources"])  # fmt: skip
    console.print(t)


@app.command()
def report(
    demo: bool = typer.Option(False, help="Use the demo database."),
    agent: bool = typer.Option(True, help="Let the auditor agent review findings (needs key)."),
    evidence: int = typer.Option(2, help="Evidence ids to show per row."),
) -> None:
    """Detect leaks, let the auditor agent verify them, print the ranked report."""
    import sys

    from leak.agent.auditor import run_audit
    from leak.ingest.scan import DEMO_DIR, open_store
    from leak.report import render

    s = load_settings(overlay=DEMO_DIR / "demo.toml" if demo else None)

    def ask(question: str) -> str:
        return str(typer.prompt(f"\n[auditor asks] {question}"))

    use_agent = agent and bool(s.gemini_api_key)
    label = f"Auditing with {s.llm.reason_model}…" if use_agent else "Running detectors…"
    with console.status(label):
        res = run_audit(s, use_agent=use_agent, ask_user=ask if sys.stdin.isatty() else None)
    store, _ = open_store(s)
    render(store, console, show_evidence=evidence)

    console.print(f"\ndetectors: {res.detector_findings} findings")
    if res.agent:
        r = res.agent
        b = load_trace_budget(r.trace_path)
        cost = f"${b.get('spent_usd', '?')} (₹{b.get('spent_inr', '?')})"
        console.print(f"auditor: {r.status} in {r.steps} steps · {cost}")
        console.print(f"trace: leak trace replay {r.run_id}")
        if r.reason:
            console.print(f"[yellow]{r.reason}[/]")
        for a in r.pending_actions:
            console.print(f"[magenta]awaiting approval[/] {a.id}: {a.tool}({a.args})")
    elif agent:
        console.print("[dim]auditor agent skipped: set GEMINI_API_KEY in .env to enable it[/]")


def load_trace_budget(path: Path | None) -> dict[str, str]:
    if not path or not path.exists():
        return {}
    end = [e for e in load_trace(path) if e.type == "run_end"]
    return dict(end[-1].data["budget"]) if end else {}


actions_app = typer.Typer(no_args_is_help=True, help="Approval queue: drafts and reminders.")
app.add_typer(actions_app, name="actions")


def _executor(demo: bool) -> Executor:
    from leak.actions.execute import Executor
    from leak.ingest.scan import DEMO_DIR, open_store

    s = load_settings(overlay=DEMO_DIR / "demo.toml" if demo else None)
    store, _ = open_store(s)
    return Executor(s, store, demo=demo)


@actions_app.command("list")
def actions_list(demo: bool = typer.Option(False, help="Use the demo database.")) -> None:
    """Show queued, approved and executed actions."""
    ex = _executor(demo)
    t = Table(title="Actions")
    for col in ("id", "status", "action", "finding", "result"):
        t.add_column(col)
    for a in ex.queue.all():
        f = ex.store.get_finding(str(a.args.get("finding_id")))
        t.add_row(a.id, a.status, a.tool, f"{f.merchant} · {f.type}" if f else "?", a.result or "")
    console.print(t)


@actions_app.command("approve")
def actions_approve(
    action_id: str, demo: bool = typer.Option(False, help="Use the demo database.")
) -> None:
    """Approve and execute one action (creates a Gmail draft / writes an .ics)."""
    ex = _executor(demo)
    a = ex.queue.get(action_id)
    if a is None:
        raise typer.BadParameter(f"no action {action_id}")
    preview = ex.preview(a)
    if preview["kind"] == "email":
        console.print(f"[bold]To:[/] {preview['to'] or '(you fill in)'}\n"
                      f"[bold]Subject:[/] {preview['subject']}\n\n{preview['body']}\n")  # fmt: skip
    else:
        console.print(f"Calendar reminder on {preview['date']}")
    if not typer.confirm("Create this?"):
        raise typer.Exit(1)
    done = ex.approve(action_id)
    color = "green" if done.status == "executed" else "red"
    console.print(f"[{color}]{done.status}[/]: {done.result}")


@actions_app.command("reject")
def actions_reject(
    action_id: str, demo: bool = typer.Option(False, help="Use the demo database.")
) -> None:
    _executor(demo).reject(action_id)
    console.print(f"rejected {action_id}")


@app.command()
def serve(
    demo: bool = typer.Option(False, help="Serve the demo database."),
    port: int = typer.Option(8765, help="Port on 127.0.0.1."),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the dashboard."),
) -> None:
    """Start the local dashboard at http://127.0.0.1:PORT (never exposed to the network)."""
    import threading
    import webbrowser

    import uvicorn

    from leak.api.app import WEB_OUT, create_app
    from leak.ingest.scan import DEMO_DIR

    s = load_settings(overlay=DEMO_DIR / "demo.toml" if demo else None)
    wanted = port
    port = _free_port(port)
    if port != wanted:
        console.print(f"[yellow]port {wanted} is in use (another `leak serve`?); using {port}[/]")
    url = f"http://127.0.0.1:{port}"
    if not WEB_OUT.is_dir():
        console.print("[yellow]dashboard not built[/]: cd web && npm install && npm run build")
    console.print(f"Leak dashboard → {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(s, demo=demo), host="127.0.0.1", port=port, log_level="warning")


@app.command()
def demo(
    serve_ui: bool = typer.Option(True, "--serve/--no-serve", help="Open the dashboard after."),
    agent: bool = typer.Option(True, help="Run the auditor agent if GEMINI_API_KEY is set (~₹30)."),
    port: int = typer.Option(8765, help="Dashboard port."),
) -> None:
    """The whole pipeline on synthetic data: scan → extract → report → dashboard."""
    from leak.ingest.scan import DEMO_DIR

    s = load_settings(overlay=DEMO_DIR / "demo.toml")
    # Start clean so every demo run tells the same story.
    for f in s.paths.data_dir.glob(s.db_name.replace(".db", "*")):
        if f.suffix in (".db", ".db-wal", ".db-shm"):  # demo.db, demo_vault.db + WAL files
            f.unlink()
    console.rule("[bold]1/3 · scan[/] synthetic inbox, statements, SMS (PII masked on the way in)")
    scan(demo=True, gmail=False, statements=None, sms=[], emails=None, limit=None)
    console.rule("[bold]2/3 · extract[/] receipts + charges (replayed from the eval cassette: ₹0)")
    extract(demo=True)
    console.rule("[bold]3/3 · report[/]")
    report(demo=True, agent=agent, evidence=1)
    if serve_ui:
        serve(demo=True, port=port, open_browser=True)


def _free_port(start: int, tries: int = 20) -> int:
    import socket

    for p in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex(("127.0.0.1", p)) != 0:
                return p
    raise typer.BadParameter(f"no free port in {start}-{start + tries - 1}; pass --port")


@gmail_app.command("auth")
def gmail_auth() -> None:
    """Open the browser to grant Leak read-only access with your own OAuth client."""
    from leak.ingest.gmail import get_credentials

    s = load_settings()
    get_credentials(s.gmail.client_secret.expanduser(), s.gmail_token_path,
                    enable_actions=s.gmail.enable_actions)  # fmt: skip
    typer.echo(f"token saved to {s.gmail_token_path} (chmod 600)")


def _settings(demo: bool) -> Settings:
    from leak.ingest.scan import DEMO_DIR

    return load_settings(overlay=DEMO_DIR / "demo.toml" if demo else None)


@trace_app.command("list")
def trace_list(demo: bool = typer.Option(False, help="Traces of the demo database.")) -> None:
    s = _settings(demo)
    for p in list_runs(s.trace_dir):
        typer.echo(p.stem)


@trace_app.command("replay")
def trace_replay(
    run_id: str, demo: bool = typer.Option(False, help="Traces of the demo database.")
) -> None:
    """Pretty-print a recorded run step by step."""
    s = _settings(demo)
    path = trace_path(s.trace_dir, run_id)
    if not path.exists():
        typer.echo(f"no trace for {run_id} in {s.trace_dir}", err=True)
        raise typer.Exit(1)
    typer.echo(render_replay(load_trace(path)))


@trace_app.command("rerun")
def trace_rerun(
    run_id: str, demo: bool = typer.Option(False, help="Traces of the demo database.")
) -> None:
    """Re-execute the demo loop using recorded LLM responses (no network, $0)."""
    s = _settings(demo)
    events = load_trace(trace_path(s.trace_dir, run_id))
    start = events[0].data
    loop = AgentLoop(
        provider=ReplayProvider(events),
        model=start["model"],
        registry=build_toy_registry(),
        budget=s.make_budget(),
        trace_dir=s.trace_dir,
        system=DEMO_SYSTEM,
        policy=PermissionPolicy(s.permissions),
    )
    result = loop.run(start["prompt"])
    typer.echo(f"rerun {result.run_id}: {result.status}\n{result.final_text}")


if __name__ == "__main__":
    app()
