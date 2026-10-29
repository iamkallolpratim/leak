"""Grounding, approvals and budgets with the real OpenRouter adapter behind a
mocked HTTP transport: what the model says is untrusted all the way through."""

import json
from decimal import Decimal

import pytest

from leak.extract.extractor import Extractor, SqliteCache
from leak.extract.schemas import BatchExtraction, DocExtraction, Receipt
from leak.harness import AgentLoop, Budget, BudgetExceeded, ModelPrice
from leak.ingest.models import Document
from leak.llm.openrouter import OpenRouterProvider
from leak.llm.provider import LLMResponse, Usage
from leak.privacy.redact import PrivacyProfile, Redactor, Vault
from leak.store import Store
from tests.helpers import MODEL, FakeOpenRouter, completion, error, make_loop, resp, tool_call
from tests.test_detect_agent import by, demo  # noqa: F401  (module fixture)

KEY = "sk-or-v1-" + "z" * 56
ROUTING = {"data_collection": "deny", "zdr": True, "require_parameters": True}


def provider(outcomes) -> tuple[OpenRouterProvider, FakeOpenRouter]:
    fake = FakeOpenRouter(outcomes)
    return OpenRouterProvider(KEY, transport=fake.transport, sleep=lambda s: None), fake


def turn(request) -> int:
    return sum(m["role"] == "assistant" for m in json.loads(request.content)["messages"])


def agent_findings(store):
    return [f for f, _, _ in store.findings(status=None) if f.source == "agent"]


# --- auditor -------------------------------------------------------------------------------


def test_untrusted_findings_are_rejected_and_actions_only_queued(demo):  # noqa: F811
    from leak.agent.auditor import detect_only, run_audit

    s, store = demo
    z = by(detect_only(store), "zombie", "cultfit")[0].model_dump(mode="json")
    fabricated = z | {"evidence": [{"evidence_id": "gmail:made-up-by-the-model"}]}
    wrong_amount = z | {"amount_inr": 4321}
    bad_date = z | {"next_renewal": "2026-02-30"}

    def model(request):
        if turn(request) == 0:
            return completion(None, tool_calls=[
                tool_call("submit_finding", {"finding": fabricated}, "c1"),
                tool_call("submit_finding", {"finding": wrong_amount}, "c2"),
                tool_call("submit_finding", {"finding": bad_date}, "c3"),
                tool_call("draft_cancellation_email", {"finding_id": z["id"]}, "c4"),
            ])  # fmt: skip
        return completion("Done.")

    p, fake = provider(model)
    r = run_audit(s, provider=p).agent
    assert r is not None and r.status == "completed", r.reason
    assert agent_findings(store) == []  # nothing the model made up was stored
    results = json.loads(fake.requests[1].content)["messages"]
    tool_msgs = [json.loads(m["content"]) for m in results if m["role"] == "tool"]
    assert "do not exist" in tool_msgs[0]["error"]
    assert "does not appear" in tool_msgs[1]["error"]
    assert "error" in tool_msgs[2]  # impossible date: schema rejects it
    # The write tool was queued for the user, not run.
    assert [a.tool for a in r.pending_actions] == ["draft_cancellation_email"]
    assert "NOT been executed" in tool_msgs[3]["result"]
    # Every request: routing on, key only in the auth header, never in the trace.
    assert all(json.loads(q.content)["provider"] == ROUTING for q in fake.requests)
    assert KEY not in r.trace_path.read_text()


def test_failed_model_call_produces_no_findings(demo):  # noqa: F811
    from leak.agent.auditor import run_audit

    s, store = demo
    p, fake = provider([error(402, "Insufficient credits")])
    r = run_audit(s, provider=p).agent
    assert r.status == "error" and "402" in (r.reason or "")
    assert agent_findings(store) == [] and len(fake.requests) == 1


def test_loop_refuses_remote_provider_without_egress(make_budget, tmp_path):
    from leak.demo_tools import build_toy_registry

    p, fake = provider([completion("hi")])
    loop = AgentLoop(provider=p, model="acme/m", registry=build_toy_registry(),
                     budget=make_budget(), trace_dir=tmp_path, system="s")  # fmt: skip
    r = loop.run("go")
    assert r.status == "error" and "egress" in (r.reason or "") and fake.requests == []


# --- budgets ------------------------------------------------------------------------------


def budget(**kw) -> Budget:
    prices = {"acme/m": ModelPrice(input=Decimal(1), output=Decimal(10))}
    return Budget(max_steps=kw.pop("max_steps", 10), max_usd=Decimal(kw.pop("max_usd", "1")),
                  prices=prices, **kw)  # fmt: skip


def test_retries_count_against_the_run_budget(tmp_path):
    from leak.demo_tools import build_toy_registry
    from leak.privacy.guard import EgressGuard

    p, fake = provider([error(503), error(503), error(503), completion("hi")])
    b = budget(max_retries=2)
    loop = AgentLoop(provider=p, model="acme/m", registry=build_toy_registry(), budget=b,
                     trace_dir=tmp_path, system="s",
                     egress=EgressGuard(Redactor(Vault(tmp_path / "v.db"))).filter)  # fmt: skip
    r = loop.run("go")
    assert r.status == "budget_exceeded" and "retry budget" in (r.reason or "")
    assert len(fake.requests) == 3 and b.retries == 2  # the 3rd retry was never sent


def test_reported_cost_is_charged_and_estimates_are_separate():
    b = budget()
    b.charge(Usage(input_tokens=10, output_tokens=10, cost_usd=Decimal("0.25")), "acme/m")
    b.charge(Usage(input_tokens=100_000, output_tokens=0), "acme/m")  # estimated: $0.10
    assert b.reported_usd == Decimal("0.25") and b.estimated_usd == Decimal("0.1")
    assert b.snapshot()["spent_usd"] == "0.350000"


def test_unknown_cost_is_never_free():
    b = budget()
    b.charge(Usage(input_tokens=5, output_tokens=5, cost_usd=Decimal("0.01")), "acme/unpriced")
    with pytest.raises(BudgetExceeded, match="refusing to count"):
        b.charge(Usage(input_tokens=5, output_tokens=5), "acme/unpriced")


def test_no_request_once_money_is_gone(make_budget, tmp_path):
    loop, scripted = make_loop([resp("x")], make_budget(max_usd="0"), tmp_path)
    assert loop.run("go").status == "budget_exceeded" and scripted.requests == []


def test_spend_from_a_failed_call_is_still_charged(make_budget, tmp_path):
    from leak.llm.provider import LLMError

    def model(msgs):
        spent = Usage(input_tokens=1000, output_tokens=0, cost_usd=Decimal("0.07"))
        raise LLMError("second JSON attempt failed", spent=spent)

    loop, _ = make_loop(model, make_budget(), tmp_path)
    r = loop.run("go")
    assert r.status == "error" and loop.budget.spent_usd == Decimal("0.07")


# --- extraction ---------------------------------------------------------------------------


@pytest.fixture
def extractor_parts(tmp_path):
    red = Redactor(Vault(tmp_path / "v.db"), PrivacyProfile())
    docs = [Document(id="gmail:1", source="gmail", title="t", raw_text="Netflix Rs 649 paid",
                     redacted_text="Netflix Rs 649 paid", content_hash="1")]  # fmt: skip
    return red, docs, SqliteCache(Store(tmp_path / "s.db").db)


def extract(outcomes, red, docs, cache, max_usd="1"):
    p, fake = provider(outcomes)
    b = Budget(max_steps=99, max_usd=Decimal(max_usd), prices={})
    results, stats = Extractor(p, "acme/m", red, cache, b).run(docs)
    return results, stats, fake, b


def batch(*docs: DocExtraction) -> str:
    return BatchExtraction(documents=list(docs)).model_dump_json()


def test_ungrounded_amounts_and_bad_dates_give_no_records(extractor_parts, tmp_path):
    red, docs, cache = extractor_parts
    invented = DocExtraction(
        evidence_id="gmail:1",
        is_financial=True,
        receipts=[Receipt(merchant="Netflix", amount=Decimal("999"))],
    )
    results, stats, fake, b = extract([completion(batch(invented))], red, docs, cache)
    assert results["gmail:1"].records() == [] and stats.dropped_ungrounded == 1
    assert b.reported_usd > 0  # OpenRouter's cost, not an estimate (no price table here)

    bad = json.loads(batch(invented))
    bad["documents"][0]["receipts"][0] |= {"amount": 649, "txn_date": "2026-13-45"}
    fresh = SqliteCache(Store(tmp_path / "fresh.db").db)  # no cached answer this time
    results, stats, fake, _ = extract([completion(json.dumps(bad))] * 2, red, docs, fresh)
    assert "gmail:1" not in results and stats.failed_docs == 1 and len(fake.requests) == 2


def test_failed_calls_give_no_records(extractor_parts):
    red, docs, cache = extractor_parts
    results, stats, fake, _ = extract([error(401)], red, docs, cache)
    assert results == {} and stats.failed_docs == 1 and stats.llm_calls == 0
    assert fake.bodies()[0]["provider"] == ROUTING


def test_extractor_checks_funds_before_sending(extractor_parts):
    red, docs, cache = extractor_parts
    _, stats, fake, _ = extract([completion("{}")], red, docs, cache, max_usd="0")
    assert fake.requests == [] and stats.stopped and "budget" in stats.stopped


def test_scripted_and_replay_providers_still_fine():
    from leak.harness import ReplayProvider
    from leak.harness.trace import TraceEvent

    recorded = {"response": LLMResponse(model=MODEL, text="hi").model_dump(mode="json")}
    ev = TraceEvent(run_id="r", seq=1, step=1, type="llm_response", ts="2026-10-29T00:00:00Z",
                    data=recorded)  # fmt: skip
    out = ReplayProvider([ev]).generate(model=MODEL, system="", messages=[],
                                        before_request=lambda a, s, m: (s, m))  # fmt: skip
    assert out.text == "hi"
