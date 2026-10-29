import base64
import email
import os

import pytest
from fastapi.testclient import TestClient

from leak.actions.drafts import DEMO_PREFIX
from leak.actions.execute import DRAFT, REMINDER, Executor
from leak.actions.queue import SqliteApprovalQueue
from leak.actions.reminders import renewal_ics
from leak.llm.provider import ToolCall


@pytest.fixture
def demo(tmp_path):
    from leak.agent.auditor import detect_only
    from leak.config import load_settings
    from leak.extract.run import demo_cache, run_extract
    from leak.ingest.scan import DEMO_DIR, demo_inputs, open_store, run_scan

    os.environ.pop("OPENROUTER_API_KEY", None)
    s = load_settings(overlay=DEMO_DIR / "demo.toml")
    os.environ.pop("OPENROUTER_API_KEY", None)
    s.paths.data_dir = tmp_path
    run_scan(s, **demo_inputs())
    run_extract(s, cache=demo_cache(s.llm.extract_model))
    store, _ = open_store(s)
    findings = detect_only(store)
    return s, store, findings


class FakeDrafts:
    def __init__(self):
        self.created = []

    def users(self):
        return self

    def drafts(self):
        return self

    def create(self, userId, body):
        self.created.append(body)
        n = len(self.created)
        return type("E", (), {"execute": lambda self_: {"id": f"r-{n}"}})()


def pick(findings, type_, mid):
    return next(f for f in findings if f.type == type_ and f.merchant_id == mid)


def test_queue_persists_and_dedupes(demo):
    _, store, findings = demo
    q = SqliteApprovalQueue(store)
    z = pick(findings, "zombie", "cultfit")
    a1 = q.enqueue("run1", DRAFT, {"finding_id": z.id})
    a2 = q.enqueue("run2", DRAFT, {"finding_id": z.id})
    assert a1.id == a2.id
    assert SqliteApprovalQueue(store).get(a1.id).status == "pending"  # new instance, same db


def test_zombie_draft_lists_charges_after_cancelling(demo):
    s, store, findings = demo
    ex = Executor(s, store, demo=False)
    d = ex.draft_for(pick(findings, "zombie", "cultfit"))
    assert d.to == "hello@cult.fit" and "Refund request" in d.subject
    assert d.body.count("Rs 1,499.00") == 3 and "15 June 2026" in d.body
    assert d.body.endswith("Asha Testkumar")  # signed with the user's real name (local only)
    demo_d = Executor(s, store, demo=True).draft_for(pick(findings, "zombie", "cultfit"))
    assert demo_d.to == "" and demo_d.subject.startswith(DEMO_PREFIX)


def test_ics(demo):
    _, _, findings = demo
    f = pick(findings, "recurring", "netflix")
    ics = renewal_ics(f)
    assert "BEGIN:VEVENT" in ics and "TRIGGER:-P3D" in ics
    assert f"DTSTART;VALUE=DATE:{f.next_renewal:%Y%m%d}" in ics
    assert all(len(line.encode()) <= 75 for line in ics.split("\r\n"))
    with pytest.raises(ValueError):
        renewal_ics(f.model_copy(update={"next_renewal": None}))


def test_approve_creates_gmail_draft_only_after_approval(demo):
    s, store, findings = demo
    fake = FakeDrafts()
    ex = Executor(s, store, demo=True, gmail=lambda: fake)
    a = ex.request(pick(findings, "zombie", "cultfit").id, DRAFT)
    assert fake.created == []  # queued, not executed
    done = ex.approve(a.id)
    assert done.status == "executed" and "r-1" in done.result
    raw = base64.urlsafe_b64decode(fake.created[0]["message"]["raw"])
    msg = email.message_from_bytes(raw)
    assert msg["Subject"].startswith(DEMO_PREFIX) and msg["To"] is None
    assert (s.paths.data_dir / "drafts" / f"{a.id}.eml").exists()
    with pytest.raises(Exception, match="already executed"):
        ex.approve(a.id)


def test_failed_action_explains_and_can_retry(demo):
    s, store, findings = demo
    s.gmail.enable_actions = False
    ex = Executor(s, store)  # real gmail factory: actions disabled -> clear error, no OAuth
    a = ex.request(pick(findings, "duplicate", "google_one").id, DRAFT)
    failed = ex.approve(a.id)
    assert failed.status == "failed" and "enable_actions" in failed.result
    fake = FakeDrafts()
    ex2 = Executor(s, store, gmail=lambda: fake)
    assert ex2.approve(a.id).status == "executed"


def test_reminder_action(demo):
    s, store, findings = demo
    ex = Executor(s, store)
    f = pick(findings, "recurring", "amazon_prime")
    done = ex.approve(ex.request(f.id, REMINDER).id)
    assert done.status == "executed"
    assert (s.paths.data_dir / "reminders" / f"{f.id}.ics").read_text().startswith("BEGIN:VCAL")


# --- API ----------------------------------------------------------------------------


@pytest.fixture
def client(demo):
    from leak.api.app import create_app

    s, _store, findings = demo
    fake = FakeDrafts()
    app = create_app(s, demo=True, token="t0k", gmail=lambda: fake)
    c = TestClient(app, base_url="http://127.0.0.1:8765")
    c.fake = fake
    c.findings = findings
    return c


H = {"X-Leak-Token": "t0k"}


def test_api_rejects_foreign_hosts_and_missing_token(client):
    evil = TestClient(client.app, base_url="http://evil.example")
    assert evil.get("/api/summary").status_code == 403  # DNS rebinding
    fid = client.findings[0].id
    assert client.post(f"/api/findings/{fid}/actions", json={"tool": DRAFT}).status_code == 403
    assert (
        client.post(
            f"/api/findings/{fid}/actions", json={"tool": DRAFT}, headers={"X-Leak-Token": "nope"}
        ).status_code
        == 403
    )
    assert (
        "access-control-allow-origin"
        not in client.get("/api/summary", headers={"Origin": "http://evil.example"}).headers
    )


def test_api_flow_review_approve_draft(client):
    s = client.get("/api/summary").json()
    assert s["potential_savings_inr"] == 24635 and s["actionable"] == 4
    fs = client.get("/api/findings").json()
    zombie = next(f for f in fs if f["type"] == "zombie")
    detail = client.get(f"/api/findings/{zombie['id']}").json()
    assert all(e["text"] for e in detail["evidence_detail"])
    assert "Asha" not in str(detail)  # masked by default

    a = client.post(f"/api/findings/{zombie['id']}/actions", json={"tool": DRAFT}, headers=H)
    assert a.status_code == 200
    queued = client.get("/api/actions").json()
    assert queued[0]["preview"]["kind"] == "email" and client.fake.created == []
    done = client.post(f"/api/actions/{a.json()['id']}/approve", headers=H).json()
    assert done["status"] == "executed" and len(client.fake.created) == 1
    assert (
        next(f for f in client.get("/api/findings").json() if f["id"] == zombie["id"])["pending"]
        == []
    )


def test_api_unmask_needs_token(client):
    eid = next(f for f in client.findings if f.type == "zombie").evidence[0].evidence_id
    assert client.get(f"/api/evidence/{eid}/unmasked").status_code == 403
    r = client.get(f"/api/evidence/{eid}/unmasked", headers=H).json()
    assert "[NAME_" not in r["text"] and "Asha" in r["text"]


def test_api_runs_and_payloads(client, demo):
    from leak.agent.auditor import run_audit
    from leak.llm.fake import ScriptedProvider
    from leak.llm.provider import LLMResponse

    s, _, _ = demo
    s.llm.prices["scripted"] = s.llm.prices[s.llm.reason_model]
    res = run_audit(s, provider=ScriptedProvider([LLMResponse(model="scripted", text="ok")]))
    runs = client.get("/api/runs").json()
    assert runs[0]["run_id"] == res.agent.run_id and runs[0]["status"] == "completed"
    payloads = client.get(f"/api/runs/{res.agent.run_id}/payloads").json()
    assert payloads[0]["system"].startswith("You are Leak's auditor")
    assert "Asha" not in str(payloads)
    assert client.get("/api/runs/..%2Fetc").status_code in (400, 404)


def test_recurring_tile_survives_auditor_dismissals(client, demo):
    """The auditor dismissing plain subscriptions must not zero 'recurring spend'."""
    _, store, findings = demo
    before = client.get("/api/summary").json()["recurring_inr"]
    assert before > 30_000
    for f in findings:
        if f.type == "recurring":
            store.set_finding_status(f.id, "dismissed", "wanted")
    after = client.get("/api/summary").json()
    assert after["recurring_inr"] == before
    assert after["potential_savings_inr"] == 24635  # savings unaffected


def test_demo_and_real_traces_live_in_different_folders(tmp_path):
    from leak.config import load_settings
    from leak.ingest.scan import DEMO_DIR

    real = load_settings()
    demo = load_settings(overlay=DEMO_DIR / "demo.toml")
    assert real.trace_dir.name == "traces"
    assert demo.trace_dir.name == "demo_traces"
    assert real.trace_dir != demo.trace_dir


def test_agent_finding_without_merchant_id_still_gets_refund_draft(demo):
    """Regression: the agent's zombie finding had merchant_id=None, so the refund
    template was skipped and the draft asked to *cancel* something already cancelled."""
    s, store, findings = demo
    z = pick(findings, "zombie", "cultfit").model_copy(update={"merchant_id": None})
    d = Executor(s, store, demo=True).draft_for(z)
    assert "Refund request" in d.subject and d.body.count("Rs 1,499.00") == 3
    assert "15 June 2026" in d.body


def test_submit_finding_fills_merchant_id(demo):
    from leak.agent.tools import build_registry

    _, store, findings = demo
    z = pick(findings, "zombie", "cultfit")
    payload = z.model_dump(mode="json") | {"merchant_id": None, "explanation": "Charged after."}
    r = build_registry(store).call(
        ToolCall(id="x", name="submit_finding", args={"finding": payload, "based_on": []})
    )
    assert not r.is_error, r.content
    saved = store.get_finding(r.content["accepted"])
    assert saved.merchant_id == "cultfit"


# --- outbound check on model-generated text ---------------------------------------

from leak.actions.execute import ActionError  # noqa: E402
from leak.privacy.guard import vet_outbound  # noqa: E402

INJECTED = "Google One. Call 9876500001 or pay at https://evil.example/pay"


@pytest.mark.parametrize(
    "texts,untrusted,expect",
    [
        ({"body": "card 4111 1111 1111 1111"}, None, "CARD"),
        ({"body": "PAN ABCPT1234F"}, None, "PAN"),
        ({"body": "call +91 98765 00001"}, None, "PHONE"),
        ({"body": "write to someone.person@gmail.com"}, None, "EMAIL"),
        ({}, {"merchant": "Netflix https://evil.example"}, "link"),
        ({}, {"merchant": "Netflix\nBcc: attacker@evil.example"}, "several lines"),
    ],
)
def test_vet_outbound_flags(texts, untrusted, expect):
    problems = vet_outbound(texts, untrusted=untrusted)
    assert any(expect in p for p in problems), problems
    # messages name the kind of data, never the value itself
    for value in ("4111", "ABCPT1234F", "98765", "someone.person", "evil.example"):
        assert all(value not in p for p in problems)


def test_vet_outbound_passes_real_drafts(demo):
    s, store, findings = demo
    ex = Executor(s, store, demo=False)
    for f in findings:
        if f.suggested_action in ("cancel", "dispute"):
            d = ex.draft_for(f)
            assert (
                vet_outbound(
                    {"subject": d.subject, "body": d.body}, untrusted={"merchant": f.merchant}
                )
                == []
            ), f.merchant
    assert vet_outbound({}, untrusted={"merchant": "[CARD_1] Google One"}) == []  # tokens are fine


def _injected(store, findings, **update):
    base = pick(findings, "duplicate", "google_one")
    f = base.model_copy(update={"merchant_id": None, **update}).with_id("injected")
    store.save_findings([f])
    return f


def test_injected_merchant_blocks_draft_before_gmail(demo):
    s, store, findings = demo
    f = _injected(store, findings, merchant=INJECTED)
    fake = FakeDrafts()
    ex = Executor(s, store, gmail=lambda: fake)
    a = ex.request(f.id, DRAFT)
    with pytest.raises(ActionError, match="outbound check"):
        ex.preview(a)
    done = ex.approve(a.id)
    assert done.status == "failed" and "contains a link" in done.result
    assert fake.created == []  # Gmail was never called
    assert "9876500001" not in done.result and "evil.example" not in done.result
    assert not (s.paths.data_dir / "drafts" / f"{a.id}.eml").exists()


def test_injected_explanation_blocks_reminder(demo):
    s, store, findings = demo
    f = _injected(store, findings, explanation="Your PAN ABCPT1234F was charged twice.")
    ex = Executor(s, store)
    done = ex.approve(ex.request(f.id, REMINDER).id)
    assert done.status == "failed" and "PAN" in done.result and "ABCPT1234F" not in done.result
    assert not (s.paths.data_dir / "reminders" / f"{f.id}.ics").exists()


def test_api_shows_blocked_preview(client, demo):
    _, store, findings = demo
    f = _injected(store, findings, merchant=INJECTED)
    client.post(f"/api/findings/{f.id}/actions", json={"tool": DRAFT}, headers=H)
    row = next(a for a in client.get("/api/actions").json() if a["args"]["finding_id"] == f.id)
    assert row["preview"]["kind"] == "error" and "outbound check" in row["preview"]["error"]


def test_submit_finding_rejects_personal_data(demo):
    from leak.agent.tools import build_registry

    _, store, findings = demo
    z = pick(findings, "zombie", "cultfit")
    payload = z.model_dump(mode="json") | {
        "explanation": "Charged on card 4111 1111 1111 1111 after cancelling."
    }
    r = build_registry(store).call(
        ToolCall(id="x", name="submit_finding", args={"finding": payload, "based_on": []})
    )
    assert r.is_error and "personal data" in r.content and "4111" not in r.content
    assert not any(f.source == "agent" for f, _, _ in store.findings(status=None))
