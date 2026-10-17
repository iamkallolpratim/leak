import os
from decimal import Decimal

import pytest

from leak.agent.tools import build_registry, readonly_query, search_documents
from leak.findings import potential_savings
from leak.llm.fake import ScriptedProvider
from leak.llm.provider import LLMResponse, ToolCall, Usage


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    from leak.config import load_settings
    from leak.extract.run import demo_cache, run_extract
    from leak.ingest.scan import DEMO_DIR, demo_inputs, open_store, run_scan

    os.environ.pop("GEMINI_API_KEY", None)
    s = load_settings(overlay=DEMO_DIR / "demo.toml")
    os.environ.pop("GEMINI_API_KEY", None)
    s.paths.data_dir = tmp_path_factory.mktemp("demo")
    s.llm.prices["scripted"] = s.llm.prices[s.llm.reason_model]
    run_scan(s, **demo_inputs())
    run_extract(s, cache=demo_cache(s.llm.extract_model))
    store, _ = open_store(s)
    return s, store


@pytest.fixture
def detected(demo):
    from leak.agent.auditor import detect_only

    _s, store = demo
    store.save_findings([], replace_source="agent")
    return detect_only(store)


# --- detectors ---------------------------------------------------------------------


def by(findings, type_, merchant_id):
    return [f for f in findings if f.type == type_ and f.merchant_id == merchant_id]


def test_every_planted_scenario_is_found(detected):
    assert by(detected, "zombie", "cultfit")[0].annual_impact_inr == Decimal(1499 * 12)
    hike = by(detected, "price_hike", "netflix")[0]
    assert hike.annual_impact_inr == Decimal((799 - 649) * 12)
    assert by(detected, "trial_conversion", "disney_hotstar")[0].amount_inr == Decimal(299)
    assert len(by(detected, "duplicate", "google_one")) == 1  # two accounts
    assert by(detected, "duplicate", "disney_hotstar")  # bundled in Airtel Black
    assert by(detected, "duplicate", "amazon_prime")
    fx = by(detected, "forex", "chatgpt")[0]
    assert Decimal(60) < fx.amount_inr < Decimal(90)
    for mid, cadence in [
        ("netflix", "monthly"),
        ("spotify", "monthly"),
        ("swiggy_one", "quarterly"),
        ("amazon_prime", "yearly"),
    ]:
        assert by(detected, "recurring", mid)[0].cadence == cadence, mid


def test_no_false_recurring_on_people_or_orders(detected):
    names = {f.merchant.lower() for f in detected if f.type == "recurring"}
    assert not any(
        x in n
        for n in names
        for x in ("ramesh", "swiggy (orders)", "salary", "bescom", "amazon (shopping)")
    )


def test_every_detector_finding_is_grounded(demo, detected):
    from leak.agent.tools import finding_grounder

    _, store = demo
    g = finding_grounder(store)
    for f in detected:
        r = g.check(f)
        assert r.ok, (f.type, f.merchant, r.errors)


def test_savings_counted_once_per_merchant(detected):
    # Hotstar is both a trial conversion and a bundled duplicate; zombie + recurring for cult.fit
    assert potential_savings(detected) == Decimal(17988 + 3588 + 1560 + 1499)


# --- read-only SQL -------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT raw_text FROM documents",
        "SELECT * FROM documents",
        "DELETE FROM ledger",
        "UPDATE ledger SET amount_inr = 0",
        "SELECT 1; DROP TABLE ledger",
        "PRAGMA table_info(ledger)",
        "ATTACH DATABASE 'x.db' AS x",
        "SELECT load_extension('x')",
        "WITH d AS (SELECT raw_text FROM documents) SELECT * FROM d",
    ],
)
def test_query_ledger_refuses(demo, sql):
    _, store = demo
    with pytest.raises(ValueError):
        readonly_query(str(store.path), sql)


def test_query_ledger_allows_analysis(demo):
    _, store = demo
    rows = readonly_query(
        str(store.path),
        "SELECT merchant_id, count(*) n, sum(amount_inr) s "
        "FROM ledger WHERE merchant_id='netflix' GROUP BY 1",
    )
    assert rows == [{"merchant_id": "netflix", "n": 6, "s": 649 * 4 + 799 * 2}]
    big = readonly_query(str(store.path), "SELECT * FROM ledger")
    assert big[-1].get("_truncated")


def test_search_and_read_evidence(demo):
    _, store = demo
    hits = search_documents(store, 'from:netflix "price is changing"')
    assert len(hits) == 1 and hits[0]["evidence_id"].startswith("gmail:")
    assert search_documents(store, "cultpass after:2026-06-01 before:2026-06-30")
    line = next(r for r in store.ledger() if r["merchant_id"] == "netflix")["evidence"].split(",")[
        0
    ]
    text = store.read_evidence(line)
    assert "> " in text and "NETFLIX" in text
    assert store.read_evidence("pdf:nope:p1:l1") is None


# --- tools via the registry ------------------------------------------------------------


def call(reg, tool, **args):
    return reg.call(ToolCall(id="x", name=tool, args=args))


def test_submit_finding_grounding(demo, detected):
    _, store = demo
    reg = build_registry(store)
    z = by(detected, "zombie", "cultfit")[0]
    good = z.model_dump(mode="json") | {"explanation": "Charged after cancelling."}
    r = call(reg, "submit_finding", finding=good, based_on=[z.id])
    assert not r.is_error and r.content["accepted"].startswith("A-")
    statuses = {f.id: st for f, st, _ in store.findings(status=None)}
    assert statuses[z.id] == "superseded"

    fake_ev = good | {"evidence": [{"evidence_id": "gmail:does-not-exist"}]}
    r = call(reg, "submit_finding", finding=fake_ev)
    assert r.is_error and "do not exist" in r.content

    wrong_amt = good | {"amount_inr": 9999}
    r = call(reg, "submit_finding", finding=wrong_amt)
    assert r.is_error and "does not appear" in r.content

    no_ev = good | {"evidence": []}
    assert call(reg, "submit_finding", finding=no_ev).is_error


def test_dismiss_and_merchant_info(demo, detected):
    _, store = demo
    reg = build_registry(store)
    rid = by(detected, "recurring", "spotify")[0].id
    assert not call(reg, "dismiss_finding", finding_id=rid, reason="wanted").is_error
    assert rid not in {f.id for f, _, _ in store.findings()}
    info = call(reg, "get_merchant_info", name="Hotstar").content
    assert info["found"] and info["id"] == "disney_hotstar"
    assert call(reg, "ask_user", question="?").content.startswith("The user is not available")


def test_auditor_run_end_to_end_scripted(demo):
    """Scripted model: inspect, submit a verified finding, dismiss one, queue a draft."""
    from leak.agent.auditor import run_audit
    from leak.report import render, report_findings

    s, store = demo
    plan = {}

    def model(msgs):
        last = msgs[-1]
        step = sum(m.role == "assistant" for m in msgs)
        if step == 0:
            return LLMResponse(
                model="scripted",
                usage=Usage(input_tokens=3000, output_tokens=300),
                tool_calls=[ToolCall(id="a", name="list_findings", args={})],
            )
        if step == 1:
            rows = last.tool_results[0].content
            z = next(r for r in rows if r["type"] == "zombie")
            sp = next(r for r in rows if r["merchant"].startswith("Spotify"))
            plan.update(z=z["id"], sp=sp["id"])
            return LLMResponse(
                model="scripted",
                tool_calls=[ToolCall(id="b", name="get_finding", args={"id": z["id"]})],
            )
        if step == 2:
            f = last.tool_results[0].content
            f["explanation"] = "Charged 3 times after cancelling on 15 Jun; ask for a refund."
            f["confidence"] = 0.95
            return LLMResponse(
                model="scripted",
                tool_calls=[
                    ToolCall(
                        id="c", name="submit_finding", args={"finding": f, "based_on": [plan["z"]]}
                    ),
                    ToolCall(
                        id="d",
                        name="dismiss_finding",
                        args={"finding_id": plan["sp"], "reason": "user listens daily"},
                    ),
                    ToolCall(
                        id="e", name="draft_cancellation_email", args={"finding_id": plan["z"]}
                    ),
                ],
            )
        return LLMResponse(model="scripted", text="Submitted 1, dismissed 1, queued 1 draft.")

    res = run_audit(s, provider=ScriptedProvider(model))
    r = res.agent
    assert r.status == "completed", r.reason
    assert [a.tool for a in r.pending_actions] == ["draft_cancellation_email"]
    shown = report_findings(store)
    assert shown[0].source == "agent" and shown[0].type == "zombie"
    assert not any(f.merchant_id == "spotify" for f in shown)
    assert all(f.evidence for f in shown)

    from io import StringIO

    from rich.console import Console

    buf = StringIO()
    render(store, Console(file=buf, width=200))
    out = buf.getvalue()
    assert "/year in potential savings" in out and "zombie" in out
    # trace contains no PII
    trace = r.trace_path.read_text()
    assert "Asha" not in trace and "ABCPT1234F" not in trace
