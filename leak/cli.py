"""Leak CLI.

Commands:
    leak scan [--demo] [--gmail] [--statements DIR] [--sms FILE] [--limit N]
    leak gmail auth                one-time OAuth with your own GCP client
    leak llm key set|replace|validate|remove   your own OpenRouter key (OS keychain)
    leak llm status                which key/models/routing this profile uses
    leak llm models                check configured models against OpenRouter's ZDR catalog
    leak demo-harness [--live]     run the harness with toy tools
    leak trace list                list recorded runs
    leak trace replay <run_id>     print a run step by step
    leak trace rerun <run_id>      re-execute the loop from recorded responses
"""

from __future__ import annotations

import os
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

from leak.actions.execute import ActionError, Executor
from leak.config import Settings, load_settings
from leak.demo_tools import build_toy_registry
from leak.harness import AgentLoop, ReplayProvider, load_trace, render_replay
from leak.harness.permissions import PermissionPolicy
from leak.harness.trace import list_runs, trace_path
from leak.llm.credentials import CredentialError, KeyStore
from leak.llm.factory import ModelConfigError, make_provider
from leak.llm.provider import LLMProvider, LLMResponse, ToolCall, Usage

app = typer.Typer(no_args_is_help=True, help="Leak: find the money leaking out of your accounts.")
trace_app = typer.Typer(no_args_is_help=True, help="Inspect and replay run traces.")
app.add_typer(trace_app, name="trace")
gmail_app = typer.Typer(no_args_is_help=True, help="Gmail connection.")
app.add_typer(gmail_app, name="gmail")
llm_app = typer.Typer(no_args_is_help=True, help="Your OpenRouter key, models and routing.")
app.add_typer(llm_app, name="llm")
key_app = typer.Typer(no_args_is_help=True, help="Store, check or remove your OpenRouter key.")
llm_app.add_typer(key_app, name="key")
console = Console()

NO_KEY_HINT = "no OpenRouter key for this profile (`leak llm key set`; `leak llm status`)"


def _provider(s: Settings) -> LLMProvider | None:
    """make_provider, with key/config problems shown as a clear one-liner."""
    try:
        return make_provider(s)
    except (CredentialError, ModelConfigError) as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1) from None


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
    live: bool = typer.Option(False, help="Use OpenRouter with your own key (`leak llm key set`)."),
    max_steps: int | None = typer.Option(None, help="Override step budget."),
) -> None:
    """Run the harness end to end with toy tools."""
    s = load_settings()
    model = s.llm.reason_model
    egress = None
    if live:
        live_provider = _provider(s)
        if live_provider is None:
            raise typer.BadParameter(
                f'--live needs [llm] provider = "openrouter" and {NO_KEY_HINT}'
            )
        provider: LLMProvider = live_provider
        from leak.ingest.scan import open_store
        from leak.privacy.guard import EgressGuard

        egress = EgressGuard(open_store(s)[1]).filter  # toy data, same guard as real runs
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
        egress=egress,
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
    from leak.ingest.statements import statement_files

    s = load_settings(overlay=DEMO_DIR / "demo.toml" if demo else None)
    inputs: dict[str, object] = demo_inputs() if demo else {}
    if not demo:
        stmt = statements or s.statements.dir
        if stmt:
            stmt = stmt.expanduser()
            inputs["statement_paths"] = statement_files(stmt)
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

    provider = _provider(s) if agent else None
    use_agent = provider is not None
    label = f"Auditing with {s.llm.reason_model}…" if use_agent else "Running detectors…"
    with console.status(label):
        res = run_audit(
            s,
            provider=provider,
            use_agent=use_agent,
            ask_user=ask if sys.stdin.isatty() else None,
        )
    store, _ = open_store(s)
    render(store, console, show_evidence=evidence)

    console.print(f"\ndetectors: {res.detector_findings} findings")
    if res.agent:
        r = res.agent
        b = load_trace_budget(r.trace_path)
        cost = (
            f"${b.get('spent_usd', '?')} (₹{b.get('spent_inr', '?')}) by Leak's meter, "
            f"${b.get('reported_usd', '?')} of it as reported by OpenRouter; "
            "your OpenRouter account has the actual bill"
        )
        console.print(f"auditor: {r.status} in {r.steps} steps · {cost}")
        console.print(f"trace: leak trace replay {r.run_id}")
        if r.reason:
            console.print(f"[yellow]{r.reason}[/]")
        for a in r.pending_actions:
            console.print(f"[magenta]awaiting approval[/] {a.id}: {a.tool}({a.args})")
    elif agent:
        console.print(f"[dim]auditor agent skipped: {NO_KEY_HINT}[/]")


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
    try:
        preview = ex.preview(a)
    except ActionError as e:
        console.print(f"[red]cannot approve {action_id}[/]: {e}")
        raise typer.Exit(1) from e
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
def card(
    demo: bool = typer.Option(False, help="Use the demo database."),
    out: Path = typer.Option(Path("leak-card.svg"), help="Where to write the SVG."),
    names: bool = typer.Option(True, "--names/--no-names", help="Show catalog merchant names."),
) -> None:
    """Write the shareable Leak Card: totals and well-known merchant names only."""
    from leak.ingest.scan import DEMO_DIR, open_store
    from leak.report import report_findings
    from leak.share import CardPrivacyError, build_card, render_svg

    s = load_settings(overlay=DEMO_DIR / "demo.toml" if demo else None)
    store, _ = open_store(s)
    try:
        c = build_card(report_findings(store), names=names)
    except CardPrivacyError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1) from e
    out.write_text(render_svg(c), encoding="utf-8")
    console.print(f"Leak Card → {out}  (PNG + share buttons: `leak serve`)")
    console.print(f"[dim]{c.share_text()}[/]")


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
    agent: bool = typer.Option(True, help="Run the auditor agent if you've set an OpenRouter key."),
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


# --- leak llm: BYOK OpenRouter key ------------------------------------------------------


def _key_store(s: Settings) -> KeyStore:
    return KeyStore(s.profile_id)


def _prompt_key() -> str:
    # Hidden prompt, never argv (shell history, `ps`). Piped stdin works for scripts.
    return str(typer.prompt("OpenRouter API key (input hidden)", hide_input=True)).strip()


def _key_info(key: str, s: Settings) -> dict[str, object]:
    """GET /key with this key: sends no prompt, no financial data."""
    from leak.llm.openrouter import OpenRouterProvider

    p = OpenRouterProvider(key, timeout=min(s.llm.timeout_s, 30.0), max_retries=0)
    try:
        return p.key_info()
    finally:
        p.close()


def _print_key_info(info: dict[str, object]) -> None:
    limit = info.get("limit_remaining")
    console.print(
        f"[green]key OK[/] · label {info.get('label') or '-'} · "
        f"credit limit left: {'unlimited' if limit is None else f'${limit}'} · "
        f"used: ${info.get('usage', '?')} · free tier: {info.get('is_free_tier')}"
    )


def _store_key(s: Settings, *, replace: bool, validate: bool) -> None:
    from leak.llm.credentials import check_key_format
    from leak.llm.openrouter import mask_key
    from leak.llm.provider import LLMError

    store = _key_store(s)
    try:
        existing = store.get()
        if existing and not replace:
            console.print("a key is already stored for this profile: `leak llm key replace`")
            raise typer.Exit(1)
        if not existing and replace:
            console.print("no key stored for this profile yet: `leak llm key set`")
            raise typer.Exit(1)
        key = check_key_format(_prompt_key())
        if validate:
            try:
                _print_key_info(_key_info(key, s))
            except LLMError as e:
                console.print(f"[red]not stored:[/] {e} (--no-validate stores it unchecked)")
                raise typer.Exit(1) from None
        store.set(key)
    except CredentialError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1) from None
    console.print(f"{'replaced' if replace else 'stored'} {mask_key(key)} in the OS keychain "
                  f"for profile {s.profile_id} ({s.paths.data_dir})")  # fmt: skip


@key_app.command("set")
def key_set(
    validate: bool = typer.Option(True, "--validate/--no-validate",
                                  help="Check the key with OpenRouter (GET /key) first."),
) -> None:  # fmt: skip
    """Store your OpenRouter key in the OS keychain (prompted, hidden)."""
    _store_key(load_settings(), replace=False, validate=validate)


@key_app.command("replace")
def key_replace(
    validate: bool = typer.Option(True, "--validate/--no-validate",
                                  help="Check the new key with OpenRouter first."),
) -> None:  # fmt: skip
    """Swap the stored key for a new one (e.g. after rotating it)."""
    _store_key(load_settings(), replace=True, validate=validate)


@key_app.command("remove")
def key_remove() -> None:
    """Delete this profile's key from the OS keychain."""
    from leak.llm.credentials import ENV_VAR

    s = load_settings()
    try:
        removed = _key_store(s).delete()
    except CredentialError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1) from None
    console.print("removed" if removed else "no key was stored for this profile")
    if os.environ.get(ENV_VAR):
        console.print(f"[yellow]{ENV_VAR} is still set in this environment and will be used[/]")


@key_app.command("validate")
def key_validate() -> None:
    """Check the active key with OpenRouter. Sends no prompt or data."""
    from leak.llm.credentials import resolve_key
    from leak.llm.provider import LLMError

    s = load_settings()
    try:
        resolved = resolve_key(KeyStore(s.profile_id), env_file_key_refused=s.env_file_key_refused)
    except CredentialError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1) from None
    if resolved is None:
        console.print(NO_KEY_HINT)
        raise typer.Exit(1)
    console.print(f"checking {resolved.masked} (from {resolved.source})…")
    try:
        _print_key_info(_key_info(resolved.key, s))
    except LLMError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1) from None


@llm_app.command("status")
def llm_status() -> None:
    """Where the key comes from (masked), models, routing. No network."""
    from leak.llm.credentials import ENV_VAR, backend_name, is_secure
    from leak.llm.openrouter import mask_key, privacy_routing

    s = load_settings()
    store = _key_store(s)
    backend = store.backend
    secure = is_secure(backend)
    try:
        stored = store.get() if secure else None
        keychain = mask_key(stored) if stored else "none"
    except CredentialError as e:
        keychain = f"unreadable ({e})"
    env = os.environ.get(ENV_VAR, "").strip()
    active = "env" if env else ("keychain" if keychain not in ("none",) and secure else "none")
    t = Table(show_header=False, box=None)
    t.add_row("provider", s.llm.provider)
    t.add_row("profile", f"{s.profile_id}  (data dir {s.paths.data_dir.expanduser().resolve()})")
    t.add_row("keychain", f"{backend_name(backend)} · {'secure' if secure else 'NOT usable'}")
    t.add_row("stored key", keychain)
    t.add_row(ENV_VAR, mask_key(env) if env else "not set")
    unused = " (unused: provider = none)" if s.llm.provider == "none" else ""
    t.add_row("active key", active + unused)
    t.add_row("extract model", s.llm.extract_model)
    t.add_row("reason model", s.llm.reason_model)
    t.add_row("routing", f"provider={privacy_routing()} on every request")
    console.print(t)
    if s.env_file_key_refused:
        console.print(f"[yellow]{ENV_VAR} in .env was ignored: Leak won't read keys from "
                      "plaintext files. Remove it and run `leak llm key set`.[/]")  # fmt: skip
    if active == "none" and s.llm.provider != "none":
        console.print(f"[dim]{NO_KEY_HINT}; Leak runs local-only until then[/]")


@llm_app.command("models")
def llm_models() -> None:
    """Check configured models against OpenRouter's live ZDR catalog (public; no key, no data)."""
    from leak.llm.openrouter import ROLE_NEEDS, check_model, fetch_catalog
    from leak.llm.provider import LLMError

    s = load_settings()
    try:
        models, zdr = fetch_catalog()
    except LLMError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1) from None
    bad = False
    for role, model in (("extract", s.llm.extract_model), ("reason", s.llm.reason_model)):
        problems = check_model(model, ROLE_NEEDS[role], models, zdr)
        needs = ", ".join(sorted(ROLE_NEEDS[role]))
        if problems:
            bad = True
            console.print(f"[red]✗ {role}[/] {model}: {'; '.join(problems)}")
        else:
            console.print(f"[green]✓ {role}[/] {model}: ZDR endpoint with {needs}")
        if model not in s.llm.prices:
            console.print("  [dim]no [llm.prices] entry: budgets rely on OpenRouter's "
                          "reported cost and stop if it is missing[/]")  # fmt: skip
    if bad:
        console.print("Pick models from https://openrouter.ai/models with a ZDR endpoint "
                      "(see README > Model compatibility).")  # fmt: skip
        raise typer.Exit(1)


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
