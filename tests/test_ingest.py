import base64
from decimal import Decimal
from pathlib import Path

import pytest

from leak.ingest.gmail import GmailSync, html_to_text, ingest_eml_dir, parse_rfc822
from leak.ingest.sms import ingest_sms_backup
from leak.ingest.statements import StatementLocked, ingest_statement, unlock_pdf
from leak.privacy.redact import PrivacyProfile, Redactor, Vault
from leak.store import Store

DEMO = Path(__file__).parents[1] / "demo" / "sample_data"
STMTS = DEMO / "statements"
PW = ["ASHA0101", "01011990"]


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "t.db")
    red = Redactor(Vault(tmp_path / "v.db"), PrivacyProfile(names=["Asha Testkumar"]))
    return store, red


@pytest.mark.parametrize(
    "file,parser,n,credits",
    [
        ("hdfc_savings_apr-sep_2026.pdf", "hdfc_savings", 50, 6),
        ("axis_savings_apr-sep_2026.pdf", "axis_savings", 25, 6),
        ("icici_credit_card_apr-sep_2026.pdf", "icici_credit_card", 47, 6),
        ("sbi_card_apr-sep_2026.pdf", "sbi_card", 31, 6),
    ],
)
def test_bank_parsers(env, file, parser, n, credits):
    store, red = env
    res = ingest_statement(STMTS / file, store=store, redactor=red, passwords=PW)
    assert (res.parser, res.transactions) == (parser, n)
    txns = store.raw_transactions()
    assert sum(t.direction == "credit" for t in txns) == credits
    # every transaction cites a real line on a stored page
    for t in txns:
        doc = store.get_document(t.document_id)
        line_no = int(t.evidence_id.rsplit(":l", 1)[1])
        line = doc.raw_text.splitlines()[line_no - 1]
        assert f"{t.amount:,.2f}".replace(",", "") in line.replace(",", "")
        assert t.account.startswith(("[ACCT_", "[CARD_"))


def test_netflix_price_hike_visible_in_hdfc(env):
    store, red = env
    ingest_statement(
        STMTS / "hdfc_savings_apr-sep_2026.pdf", store=store, redactor=red, passwords=PW
    )
    netflix = [t.amount for t in store.raw_transactions() if "NETFLIX" in t.description]
    assert netflix == [Decimal(649)] * 4 + [Decimal(799)] * 2


def test_icici_foreign_currency(env):
    store, red = env
    ingest_statement(STMTS / "icici_credit_card_apr-sep_2026.pdf", store=store, redactor=red)
    usd = [t for t in store.raw_transactions() if t.foreign_currency == "USD"]
    assert len(usd) == 6 and all(t.foreign_amount == 20 for t in usd)


def test_locked_pdf_needs_password():
    with pytest.raises(StatementLocked):
        unlock_pdf(STMTS / "sbi_card_apr-sep_2026.pdf", ["wrong"])
    assert unlock_pdf(STMTS / "sbi_card_apr-sep_2026.pdf", ["01011990"]).startswith(b"%PDF")


def test_unknown_bank_without_fallback_is_refused(env, tmp_path):
    from fpdf import FPDF

    from leak.ingest.statements import UnsupportedStatement

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Courier", size=8)
    pdf.cell(0, 5, "MAHANAGAR CO-OP BANK  01-04-2026 SOMETHING 100.00")
    path = tmp_path / "x.pdf"
    pdf.output(str(path))
    with pytest.raises(UnsupportedStatement):
        ingest_statement(path, store=env[0], redactor=env[1])


def test_eml_parsing_and_html_only(env):
    store, red = env
    assert ingest_eml_dir(DEMO / "emails", store=store, redactor=red) == 22
    doc = store.get_document("gmail:demo-0001")
    assert "Netflix" in doc.redacted_text and doc.ts is not None
    assert html_to_text("<p>a<b>b</b></p><script>x()</script><div>c</div>") == "ab\n\nc"
    raw = (
        b"From: x@y.com\r\nSubject: hi\r\nContent-Type: text/html\r\n\r\n"
        b"<html><body><p>Rs 299</p></body></html>"
    )
    assert parse_rfc822(raw).body == "Rs 299"


def test_sms_filtering(env):
    store, red = env
    stats = ingest_sms_backup(DEMO / "sms" / "sms-backup-demo.xml", store=store, redactor=red)
    assert stats.kept == 21 and stats.skipped_personal == 1 and stats.skipped_otp == 1
    bodies = [d.raw_text for d in store.documents("sms")]
    assert not any("dinner" in b or "OTP" in b for b in bodies)


class FakeGmail:
    def __init__(self, messages):
        self.messages_data = messages
        self.queries = []

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, userId, q, maxResults, pageToken=None):
        self.queries.append(q)
        ids = [{"id": i} for i in self.messages_data]
        return _Exec({"messages": ids})

    def get(self, userId, id, format):
        assert format == "raw"
        return _Exec({"raw": base64.urlsafe_b64encode(self.messages_data[id]).decode()})


class _Exec:
    def __init__(self, v):
        self.v = v

    def execute(self):
        return self.v


def test_gmail_incremental_sync(env):
    store, red = env
    eml = (DEMO / "emails" / "demo-0001.eml").read_bytes()
    svc = FakeGmail({"m1": eml, "m2": eml})
    s1 = GmailSync(svc, store=store, redactor=red, query="q").sync()
    assert (s1.fetched, s1.skipped_known) == (2, 0)
    s2 = GmailSync(svc, store=store, redactor=red, query="q").sync()
    assert (s2.fetched, s2.skipped_known) == (0, 2)
    assert svc.queries[0] == "q" and svc.queries[1].startswith("q after:")


# --- progress + --limit ---------------------------------------------------------


class Recorder:
    def __init__(self):
        self.events = []

    def start(self, key, label, total):
        self.events.append(("start", key, total))

    def advance(self, key, n=1, detail=""):
        self.events.append(("advance", key, n))

    def finish(self, key, summary=""):
        self.events.append(("finish", key, summary))

    def advanced(self, key):
        return sum(e[2] for e in self.events if e[0] == "advance" and e[1] == key)


class PagedGmail(FakeGmail):
    """Newest-first ids, honours maxResults + pageToken; ids in `bad` return junk."""

    def __init__(self, messages, bad=()):
        super().__init__(messages)
        self.bad = set(bad)
        self.page_sizes = []

    def list(self, userId, q, maxResults, pageToken=None):
        self.queries.append(q)
        self.page_sizes.append(maxResults)
        ids = list(self.messages_data)
        start = int(pageToken or 0)
        page = ids[start : start + maxResults]
        nxt = start + maxResults
        resp = {"messages": [{"id": i} for i in page]}
        if nxt < len(ids):
            resp["nextPageToken"] = str(nxt)
        return _Exec(resp)

    def get(self, userId, id, format):
        if id in self.bad:
            return _Exec({})  # missing "raw" -> KeyError inside sync
        return super().get(userId, id, format)


@pytest.fixture
def five_msgs():
    eml = (DEMO / "emails" / "demo-0001.eml").read_bytes()
    return {f"m{i}": eml for i in range(5)}


def test_limit_fetches_newest_n_and_keeps_cursor(env, five_msgs):
    store, red = env
    svc = PagedGmail(five_msgs)
    stats = GmailSync(svc, store=store, redactor=red, query="q", limit=2).sync()
    assert stats.fetched == 2 and stats.limited and not stats.cursor_updated
    assert {d.id for d in store.documents("gmail")} == {"gmail:m0", "gmail:m1"}
    assert svc.page_sizes == [2]  # never asked Gmail for more than needed
    assert store.get_cursor("gmail") is None
    # A later full run still sees the whole range and only fetches the rest.
    full = GmailSync(svc, store=store, redactor=red, query="q").sync()
    assert (full.fetched, full.skipped_known, full.cursor_updated) == (3, 2, True)
    assert svc.queries[-1] == "q"


def test_bad_message_is_skipped_and_cursor_held(env, five_msgs):
    store, red = env
    stats = GmailSync(
        PagedGmail(five_msgs, bad={"m3"}), store=store, redactor=red, query="q"
    ).sync()
    assert (stats.fetched, stats.errors) == (4, 1)
    assert stats.first_error.startswith("m3: KeyError")
    assert not stats.cursor_updated and store.get_cursor("gmail") is None


def test_paging_and_progress_events(env, five_msgs):
    store, red = env
    rec = Recorder()
    sync = GmailSync(
        PagedGmail(five_msgs), store=store, redactor=red, query="q", max_messages=4, progress=rec
    )
    stats = sync.sync()
    assert stats.fetched == 4
    assert ("start", "gmail-list", None) in rec.events
    assert ("start", "gmail", 4) in rec.events
    assert rec.advanced("gmail-list") == 4 and rec.advanced("gmail") == 4
    assert [e[1] for e in rec.events if e[0] == "finish"] == ["gmail-list", "gmail"]


def test_eml_limit_takes_newest_by_date(env):
    store, red = env
    rec = Recorder()
    n = ingest_eml_dir(DEMO / "emails", store=store, redactor=red, limit=3, progress=rec)
    assert n == 3 and rec.advanced("eml") == 3
    all_dates = sorted(
        (parse_rfc822(p.read_bytes()).date for p in (DEMO / "emails").glob("*.eml")), reverse=True
    )
    assert sorted((d.ts for d in store.documents("gmail")), reverse=True) == all_dates[:3]


def test_run_scan_reports_progress_for_every_source(tmp_path, monkeypatch):
    from leak.config import load_settings
    from leak.ingest.scan import DEMO_DIR, demo_inputs, run_scan

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    s = load_settings(overlay=DEMO_DIR / "demo.toml")
    s.paths.data_dir = tmp_path
    rec = Recorder()
    report = run_scan(s, **demo_inputs(), email_limit=4, progress=rec)
    assert report.emails == 4
    assert rec.advanced("statements") == 4
    assert rec.advanced("sms:sms-backup-demo.xml") == 24


def test_cli_limit_flag(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from leak.cli import app

    monkeypatch.setenv("LEAK_CONFIG", str(Path(__file__).parents[1] / "config.example.toml"))
    monkeypatch.chdir(tmp_path)  # data_dir ".leak" lands in tmp
    out = CliRunner().invoke(app, ["scan", "--demo", "-n", "3"])
    assert out.exit_code == 0, out.output
    assert "3 emails" in out.output
    assert CliRunner().invoke(app, ["scan", "--demo", "--limit", "0"]).exit_code != 0
