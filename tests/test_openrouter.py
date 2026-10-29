"""OpenRouter provider, at the HTTP layer: mocks only, no key, no network."""

import json
import logging
from decimal import Decimal

import httpx2 as httpx
import pytest
from pydantic import BaseModel

from leak.llm.openrouter import (
    BASE_URL,
    BadRequest,
    ContextTooLong,
    Forbidden,
    InsufficientCredits,
    InvalidKey,
    MalformedOutput,
    MissingGate,
    OpenRouterProvider,
    PrivacyRoutingUnavailable,
    ProviderUnavailable,
    RateLimited,
    RequestTimeout,
    UnknownModel,
    check_model,
    mask_key,
    to_openai_messages,
)
from leak.llm.provider import LLMError, Message, ToolCall, ToolResult, ToolSpec
from leak.privacy.guard import EgressGuard, GuardedProvider, PrivacyViolation
from leak.privacy.redact import Redactor, Vault
from tests.helpers import FakeOpenRouter, completion, error, tool_call

KEY = "sk-or-v1-" + "a" * 56
MODEL = "google/gemini-3.5-flash-lite"
ROUTING = {"data_collection": "deny", "zdr": True, "require_parameters": True}
USER = [Message(role="user", text="hello")]


def passthrough(attempt, system, messages):
    return system, messages


def make(outcomes, **kw):
    fake = FakeOpenRouter(outcomes)
    sleeps: list[float] = []
    p = OpenRouterProvider(KEY, transport=fake.transport, sleep=sleeps.append, **kw)
    return p, fake, sleeps


def gen(p, **kw):
    kw.setdefault("before_request", passthrough)
    return p.generate(model=MODEL, system="sys", messages=kw.pop("messages", USER), **kw)


# --- request shape ---------------------------------------------------------------------


def test_base_url_auth_model_and_routing():
    p, fake, _ = make([completion("hi")])
    r = gen(p)
    req = fake.requests[0]
    assert str(req.url) == f"{BASE_URL}/chat/completions"
    assert req.headers["authorization"] == f"Bearer {KEY}"
    assert "openai-organization" not in req.headers
    body = fake.bodies()[0]
    assert body["model"] == MODEL
    assert body["provider"] == ROUTING
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert r.text == "hi" and r.model == MODEL and r.served_by == "Google"


def test_org_env_is_not_forwarded(monkeypatch):
    monkeypatch.setenv("OPENAI_ORG_ID", "org-secret")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "proj-secret")
    p, fake, _ = make([completion()])
    gen(p)
    h = fake.requests[0].headers
    assert "openai-organization" not in h and "openai-project" not in h


def test_usage_and_reported_cost():
    p, _, _ = make([completion(prompt=1200, out=80, cached=400, cost=0.00042)])
    u = gen(p).usage
    assert (u.input_tokens, u.output_tokens, u.cached_tokens) == (1200, 80, 400)
    assert u.cost_usd == Decimal("0.00042")


def test_missing_usage_is_estimated_never_zero():
    resp = completion("some answer text")
    data = json.loads(resp.content)
    del data["usage"]
    p, _, _ = make([httpx.Response(200, json=data)])
    u = gen(p).usage
    assert u.cost_usd is None and u.input_tokens > 0 and u.output_tokens > 0


def test_tools_and_reasoning_details_round_trip():
    details = [{"type": "reasoning.encrypted", "data": "c2ln", "format": "google-gemini-v1"}]
    p, fake, _ = make(
        [completion(None, tool_calls=[tool_call("add", {"a": 2})], reasoning_details=details)]
    )
    tools = [ToolSpec(name="add", description="add", parameters={"type": "object"})]
    r = gen(p, tools=tools)
    assert fake.bodies()[0]["tools"][0]["function"]["name"] == "add"
    call = r.tool_calls[0]
    assert call.args == {"a": 2} and json.loads(call.provider_meta["reasoning_details"]) == details
    # Sent back verbatim on the next turn, with the tool result.
    msgs = to_openai_messages(
        "",
        [
            Message(role="assistant", tool_calls=[call]),
            Message(role="tool", tool_results=[ToolResult(call_id=call.id, name="add", content=2)]),
        ],
    )
    assert msgs[0]["reasoning_details"] == details
    assert msgs[0]["tool_calls"][0]["function"] == {"name": "add", "arguments": '{"a": 2}'}
    assert msgs[1] == {"role": "tool", "tool_call_id": call.id, "content": '{"result": 2}'}


class Out(BaseModel):
    n: int


def test_structured_output_and_one_reask_through_the_gate():
    seen: list[int] = []

    def gate(attempt, system, messages):
        seen.append(attempt)
        return system, messages

    p, fake, _ = make([completion('{"n": "x"}', cost=0.001), completion('{"n": 3}', cost=0.002)])
    r = gen(p, response_schema=Out, before_request=gate)
    assert r.text == '{"n": 3}' and seen == [0, 1]
    first, second = fake.bodies()
    assert first["response_format"]["type"] == "json_schema"
    assert first["response_format"]["json_schema"]["name"] == "Out"
    assert first["provider"] == second["provider"] == ROUTING
    assert "failed validation" in second["messages"][-1]["content"]
    assert '"x"' not in second["messages"][-1]["content"]  # values are not echoed back
    assert r.usage.cost_usd == Decimal("0.003")


def test_invalid_json_twice_raises_and_reports_spend():
    p, _, _ = make([completion('{"n": "x"}'), completion("not json")])
    with pytest.raises(MalformedOutput, match="invalid JSON twice") as e:
        gen(p, response_schema=Out)
    assert e.value.spent is not None and e.value.spent.cost_usd == Decimal("0.0024")


# --- retries --------------------------------------------------------------------------


def test_routing_on_every_retry_and_retry_after_is_honoured():
    p, fake, sleeps = make(
        [error(429, headers={"retry-after": "3"}), error(502), completion("ok")], base_delay=1.0
    )
    seen: list[int] = []
    r = gen(p, before_request=lambda a, s, m: (seen.append(a), (s, m))[1])
    assert r.text == "ok" and len(fake.requests) == 3 and seen == [0, 1, 2]
    assert all(b["provider"] == ROUTING for b in fake.bodies())
    assert 3.0 <= sleeps[0] <= 4.0  # Retry-After + jitter
    assert 0.0 <= sleeps[1] <= 2.0  # backoff for the 2nd retry: up to base * 2


def test_backoff_is_bounded():
    p, fake, sleeps = make([error(503)] * 10, max_retries=3, base_delay=1.0, max_delay=2.5)
    with pytest.raises(ProviderUnavailable):
        gen(p)
    assert len(fake.requests) == 4 and len(sleeps) == 3
    assert all(0 <= s <= 2.5 for s in sleeps)


def test_too_long_retry_after_fails_instead_of_waiting():
    p, fake, sleeps = make([error(429, headers={"retry-after": "3600"}), completion()])
    with pytest.raises(RateLimited):
        gen(p)
    assert len(fake.requests) == 1 and sleeps == []


def test_network_errors_and_timeouts_are_retried_then_raised():
    p, fake, _ = make([httpx.ConnectError("down"), completion("ok")])
    assert gen(p).text == "ok" and len(fake.requests) == 2
    p, fake, _ = make([httpx.ReadTimeout("slow")] * 3, max_retries=2)
    with pytest.raises(RequestTimeout):
        gen(p)
    assert len(fake.requests) == 3


@pytest.mark.parametrize(
    ("resp", "exc"),
    [
        (error(400, "bad things"), BadRequest),
        (error(401, "No auth credentials found"), InvalidKey),
        (error(402, "Insufficient credits"), InsufficientCredits),
        (error(403, "flagged by moderation"), Forbidden),
        (error(404, "Model google/nope not found"), UnknownModel),
        (error(400, "google/nope is not a valid model ID"), UnknownModel),
        (error(400, "This endpoint's maximum context length is 1000 tokens"), ContextTooLong),
        (error(404, "No endpoints found matching your data policy"), PrivacyRoutingUnavailable),
    ],
)
def test_permanent_errors_are_not_retried(resp, exc):
    p, fake, sleeps = make([resp, completion()])
    with pytest.raises(exc) as e:
        gen(p)
    assert len(fake.requests) == 1 and sleeps == []
    assert fake.bodies()[0]["provider"] == ROUTING  # routing was asked for, not dropped
    assert KEY not in str(e.value)


def test_privacy_routing_503_retries_with_same_constraints_then_fails_closed():
    msg = "No available provider meets your routing requirements"
    p, fake, _ = make([error(503, msg)] * 3, max_retries=2)
    with pytest.raises(PrivacyRoutingUnavailable, match="will not relax"):
        gen(p)
    assert len(fake.requests) == 3
    assert all(b["provider"] == ROUTING for b in fake.bodies())


def test_error_in_200_body_and_empty_answers_are_never_success():
    body_err = httpx.Response(200, json={"error": {"code": 502, "message": "upstream died"}})
    p, _, _ = make([body_err] * 5)
    with pytest.raises(ProviderUnavailable):
        gen(p)
    p, fake, _ = make([httpx.Response(200, json={"choices": []}), completion("late")])
    assert gen(p).text == "late" and len(fake.requests) == 2  # cold start: retried
    p, _, _ = make([completion("")] * 5)
    with pytest.raises(MalformedOutput, match="empty"):
        gen(p)
    p, _, _ = make([completion("", finish="length")])
    with pytest.raises(MalformedOutput, match="output limit"):
        gen(p)
    p, _, _ = make([completion(None, tool_calls=[{"id": "c", "type": "function",
                    "function": {"name": "add", "arguments": "{oops"}}])])  # fmt: skip
    with pytest.raises(MalformedOutput, match="not JSON"):
        gen(p)


# --- gate / egress ------------------------------------------------------------------------


def test_no_gate_no_request():
    p, fake, _ = make([completion()])
    with pytest.raises(MissingGate):
        p.generate(model=MODEL, system="", messages=USER)
    assert fake.requests == []


def test_gate_runs_before_every_attempt_and_can_block_a_retry():
    def gate(attempt, system, messages):
        if attempt > 0:
            raise LLMError("retry budget exhausted")
        return system, messages

    p, fake, _ = make([error(500), completion()])
    with pytest.raises(LLMError, match="retry budget"):
        gen(p, before_request=gate)
    assert len(fake.requests) == 1  # the retry never went out


def test_egress_redacts_and_rejects_before_any_http(tmp_path):
    guard = EgressGuard(Redactor(Vault(tmp_path / "v.db")))
    p, fake, _ = make([completion("ok")])
    GuardedProvider(p, guard).generate(
        model=MODEL, system="s", messages=[Message(role="user", text="card 4111 1111 1111 1111")]
    )
    sent = fake.requests[0].content.decode()
    assert "4111 1111 1111 1111" not in sent and "[CARD_" in sent

    class Leaky(EgressGuard):
        def filter(self, system, messages):
            raise PrivacyViolation("refusing to send payload: unredacted ['pan']")

    p, fake, _ = make([completion()])
    with pytest.raises(PrivacyViolation):
        GuardedProvider(p, Leaky(guard.redactor)).generate(model=MODEL, system="", messages=USER)
    assert fake.requests == []


def test_guard_reruns_on_retries(tmp_path):
    calls: list[int] = []

    class Counting(EgressGuard):
        def filter(self, system, messages):
            calls.append(1)
            return super().filter(system, messages)

    p, fake, _ = make([error(502), error(429), completion()])
    guard = Counting(Redactor(Vault(tmp_path / "v.db")))
    GuardedProvider(p, guard).generate(model=MODEL, system="", messages=USER)
    assert len(fake.requests) == 3 and len(calls) == 1 + 3  # up front + once per attempt


# --- secrets ----------------------------------------------------------------------------------


def test_key_never_in_repr_logs_or_errors(caplog):
    caplog.set_level(logging.DEBUG)
    p, _, _ = make([completion("ok"), error(401)])
    gen(p, messages=[Message(role="user", text="PROMPT-MARKER")])
    with pytest.raises(InvalidKey) as e:
        gen(p)
    assert KEY not in repr(p) and KEY not in str(e.value)
    assert KEY not in caplog.text and "PROMPT-MARKER" not in caplog.text
    assert mask_key(KEY) == "sk-or-v1-…aaaa"


def test_key_info_sends_no_body():
    p, fake, _ = make(
        [httpx.Response(200, json={"data": {"label": "mine", "limit_remaining": 4.5}})]
    )
    assert p.key_info()["label"] == "mine"
    req = fake.requests[0]
    assert str(req.url) == f"{BASE_URL}/key" and req.method == "GET" and not req.content
    p, _, _ = make([error(401)])
    with pytest.raises(InvalidKey):
        p.key_info()


def test_timeout_must_be_finite():
    with pytest.raises(ValueError):
        OpenRouterProvider(KEY, timeout=float("inf"))
    with pytest.raises(InvalidKey):
        OpenRouterProvider("  ")


# --- model compatibility ---------------------------------------------------------------------


def test_check_model():
    models = [{"id": MODEL}, {"id": "acme/no-zdr"}, {"id": "acme/no-tools"}]
    zdr = [
        {"model_id": MODEL, "supported_parameters": ["tools", "structured_outputs"]},
        {"model_id": "acme/no-tools", "supported_parameters": ["response_format"]},
    ]
    assert check_model(MODEL, {"tools"}, models, zdr) == []
    assert check_model(MODEL, {"response_format"}, models, zdr) == []
    assert "not an OpenRouter id" in check_model("gemini-x", {"tools"}, models, zdr)[0]
    assert "catalog" in check_model("acme/ghost", {"tools"}, models, zdr)[0]
    assert "Zero Data Retention" in check_model("acme/no-zdr", {"tools"}, models, zdr)[0]
    assert "supports tools" in check_model("acme/no-tools", {"tools"}, models, zdr)[0]


def test_tool_call_meta_is_only_on_first_call():
    calls = [
        ToolCall(id="1", name="a", provider_meta={"reasoning_details": "[]"}),
        ToolCall(id="2", name="b"),
    ]
    out = to_openai_messages("", [Message(role="assistant", tool_calls=calls)])
    assert out[0]["reasoning_details"] == [] and len(out[0]["tool_calls"]) == 2
