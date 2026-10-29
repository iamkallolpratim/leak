"""Phase 2 acceptance: no unmasked PAN / card / account / phone (or name, email,
address) ever appears in a payload handed to an LLM provider."""

import json

import pytest
from pydantic import BaseModel

from leak.config import load_settings
from leak.harness import AgentLoop, Budget, ModelPrice, ToolRegistry
from leak.ingest.scan import DEMO_DIR, demo_inputs, open_store, run_scan
from leak.ingest.statements import LLMStatementParser, ingest_statement
from leak.llm.fake import ScriptedProvider
from leak.llm.provider import LLMResponse, ToolCall, Usage
from leak.privacy.guard import EgressGuard, GuardedProvider, PrivacyViolation
from leak.privacy.redact import find_pii

# Every raw identifier planted in the synthetic demo data (see demo/generate_sample_data.py)
RAW_PII = [
    "Asha",
    "Testkumar",
    "asha.testkumar@example.com",
    "asha.family.test@example.com",
    "98765 00001",
    "9876500001",
    "ABCPT1234F",
    "50100123456789",
    "918010012345678",
    "1203455512",
    "4315 XXXX XXXX 1007",
    "XX1007",
    "XXXX XXXX XXXX 4444",
    "**6789",
    "XX5678",
    "416021XXXXXX6789",
    "ending 4444",
    "ending 1007",
    "Synthetic Heights",
    "Testnagar",
    "560001",
    "ramesh.ll@okicici",
]


def assert_clean(payload: str) -> None:
    for value in RAW_PII:
        assert value not in payload, f"raw PII leaked: {value!r}"


@pytest.fixture
def demo_settings(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    s = load_settings(overlay=DEMO_DIR / "demo.toml")
    s.paths.data_dir = tmp_path
    return s


def test_all_stored_redacted_text_is_clean(demo_settings):
    run_scan(demo_settings, **demo_inputs())
    store, red = open_store(demo_settings)
    docs = store.documents()
    assert len(docs) == 22 + 6 + 21
    for d in docs:
        for field in (d.redacted_text, d.title, d.sender):
            assert_clean(field)
            assert find_pii(field, red.profile) == [], (d.id, find_pii(field, red.profile))
    for t in store.raw_transactions():
        assert_clean(t.description + t.account)
    # ...while the raw text is still there locally, and the vault can unmask.
    hdfc = next(d for d in docs if d.title.startswith("HDFC"))
    assert "50100123456789" in hdfc.raw_text
    assert "[ACCT_" in hdfc.redacted_text and "6789" in red.vault.unmask(hdfc.redacted_text)


class Args(BaseModel):
    doc_id: str


def test_agent_loop_payloads_clean_even_with_a_leaky_tool(demo_settings, tmp_path):
    """A careless tool returns RAW text. The egress guard must catch it."""
    run_scan(demo_settings, **demo_inputs())
    store, red = open_store(demo_settings)
    docs = store.documents()

    reg = ToolRegistry()

    @reg.tool(description="BAD tool: returns raw, unredacted text")
    def read_raw(args: Args) -> str:
        doc = store.get_document(args.doc_id)
        return doc.raw_text if doc else ""

    calls = [
        ToolCall(id=f"c{i}", name="read_raw", args={"doc_id": d.id}) for i, d in enumerate(docs)
    ]
    provider = ScriptedProvider(
        [
            LLMResponse(model="m", tool_calls=calls),
            LLMResponse(model="m", text="done"),
        ]
    )
    loop = AgentLoop(
        provider=provider,
        model="m",
        registry=reg,
        trace_dir=tmp_path / "traces",
        budget=Budget(max_steps=5, max_usd=1, prices={"m": ModelPrice(input=0, output=0)}),
        system="Audit for Asha Testkumar, phone 9876500001",  # even the system prompt
        egress=EgressGuard(red).filter,
        trace_scrub=EgressGuard(red).scrub,
    )
    result = loop.run("Look at a/c 50100123456789 and card 4315 XXXX XXXX 1007")
    assert result.status == "completed"

    sent = json.dumps([[m.model_dump(mode="json") for m in req] for req in provider.requests])
    assert_clean(sent)
    assert "[PHONE_1]" in sent or "[PHONE_" in sent  # it was masked, not dropped
    # The trace (what the UI will show as "what the LLM saw") is clean too.
    assert_clean(result.trace_path.read_text())


def test_guard_fails_closed(demo_settings):
    _, red = open_store(demo_settings)
    guard = EgressGuard(red)
    red.redact = lambda text: text  # simulate a redaction bug
    inner = ScriptedProvider([LLMResponse(model="m", text="never")])
    from leak.llm.provider import Message

    with pytest.raises(PrivacyViolation):
        GuardedProvider(inner, guard).generate(
            model="m", system="", messages=[Message(role="user", text="PAN ABCPT1234F")]
        )
    assert inner.requests == []  # nothing was sent


def test_llm_statement_fallback_sees_only_redacted_text(demo_settings, tmp_path):
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Courier", size=8)
    for line in [
        "MAHANAGAR CO-OPERATIVE BANK",
        "ASHA TESTKUMAR",
        "Flat 12B, Synthetic Heights",
        "Mobile +91 98765 00001  PAN ABCPT1234F",
        "Account Number: 50100123456789",
        "",
        "02-05-2026 NETFLIX SUBSCRIPTION 649.00 DR",
        "09-05-2026 SALARY CREDIT 95,000.00 CR",
    ]:
        pdf.cell(0, 5, line, new_x="LMARGIN", new_y="NEXT")
    path = tmp_path / "mahanagar.pdf"
    pdf.output(str(path))
    from leak.ingest.statements import pdf_pages_text, unlock_pdf

    lines = pdf_pages_text(unlock_pdf(path))[0].splitlines()
    ln = {k: next(i for i, t in enumerate(lines, 1) if k in t) for k in ("NETFLIX", "SALARY")}

    answer = {
        "bank_name": "Mahanagar Co-op Bank",
        "is_credit_card": False,
        "account_token": "[ACCT_1]",
        "transactions": [
            {
                "page": 1,
                "line": ln["NETFLIX"],
                "txn_date": "2026-05-02",
                "description": "NETFLIX",
                "amount": "649.00",
                "direction": "debit",
            },
            {
                "page": 1,
                "line": ln["SALARY"],
                "txn_date": "2026-05-09",
                "description": "SALARY",
                "amount": "95000.00",
                "direction": "credit",
            },
            # hallucinated row: cites a line that doesn't contain the amount
            {
                "page": 1,
                "line": 2,
                "txn_date": "2026-05-10",
                "description": "FAKE",
                "amount": "999.00",
                "direction": "debit",
            },
        ],
    }
    inner = ScriptedProvider(
        [
            LLMResponse(
                model="flash",
                text=json.dumps(answer),
                usage=Usage(input_tokens=500, output_tokens=200),
            )
        ]
    )
    store, red = open_store(demo_settings)
    budget = Budget(max_steps=5, max_usd=1, prices={"flash": ModelPrice(input=1, output=1)})
    res = ingest_statement(
        path,
        store=store,
        redactor=red,
        fallback=LLMStatementParser(inner, "flash", red, budget=budget),
    )
    assert res.parser == "llm_fallback" and res.transactions == 2  # fake row dropped
    assert budget.spent_usd > 0
    assert_clean(json.dumps([[m.model_dump(mode="json") for m in r] for r in inner.requests]))
