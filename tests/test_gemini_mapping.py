"""Gemini provider tests with a fake client -- no network, no key."""

import pytest
from google.genai import errors, types
from pydantic import BaseModel

from leak.llm.gemini import GeminiProvider, to_content
from leak.llm.provider import LLMError, Message, ToolCall, ToolResult, ToolSpec


def _raw(parts, prompt=50, out=7, thoughts=3, cached=10):
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=parts),
                finish_reason=types.FinishReason.STOP,
            )
        ],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=prompt,
            candidates_token_count=out,
            thoughts_token_count=thoughts,
            cached_content_token_count=cached,
        ),
    )


class FakeModels:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        o = self.outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        return o


class FakeClient:
    def __init__(self, outcomes):
        self.models = FakeModels(outcomes)


def provider(outcomes):
    client = FakeClient(outcomes)
    sleeps: list[float] = []
    return GeminiProvider(client=client, sleep=sleeps.append), client, sleeps


TOOLS = [
    ToolSpec(
        name="add",
        description="add",
        parameters={"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]},
    )
]


def test_function_call_response_maps_to_toolcall():
    part = types.Part(
        function_call=types.FunctionCall(id="fc1", name="add", args={"a": 1}),
        thought_signature=b"sig-bytes",
    )
    p, client, _ = provider([_raw([types.Part(text="ok "), part])])
    r = p.generate(model="m", system="sys", messages=[Message(role="user", text="hi")], tools=TOOLS)
    assert r.text == "ok "
    assert r.tool_calls[0].name == "add" and r.tool_calls[0].args == {"a": 1}
    assert r.usage.input_tokens == 50 and r.usage.output_tokens == 10  # out + thinking
    assert r.usage.cached_tokens == 10
    cfg = client.models.calls[0]["config"]
    assert cfg.system_instruction == "sys"
    assert cfg.automatic_function_calling.disable is True
    assert cfg.tools[0].function_declarations[0].parameters_json_schema == TOOLS[0].parameters

    # Thought signature round-trips back into the next request.
    content = to_content(Message(role="assistant", tool_calls=r.tool_calls))
    assert content.role == "model"
    assert content.parts[0].thought_signature == b"sig-bytes"


def test_tool_results_become_function_responses():
    c = to_content(
        Message(
            role="tool",
            tool_results=[
                ToolResult(call_id="fc1", name="add", content=3),
                ToolResult(call_id="fc2", name="x", content="boom", is_error=True),
            ],
        )
    )
    assert c.role == "user"
    assert c.parts[0].function_response.response == {"result": 3}
    assert c.parts[1].function_response.response == {"error": "boom"}


def test_retries_on_429_then_succeeds():
    rate_limited = errors.ClientError(429, {"error": {"message": "slow down"}})
    p, client, sleeps = provider([rate_limited, rate_limited, _raw([types.Part(text="hi")])])
    r = p.generate(model="m", system="", messages=[Message(role="user", text="x")])
    assert r.text == "hi" and len(client.models.calls) == 3 and len(sleeps) == 2


def test_non_retryable_error_raises_immediately():
    p, client, _ = provider([errors.ClientError(400, {"error": {"message": "bad"}})])
    with pytest.raises(LLMError, match="400"):
        p.generate(model="m", system="", messages=[Message(role="user", text="x")])
    assert len(client.models.calls) == 1


def test_gives_up_after_max_retries():
    err = errors.ServerError(503, {"error": {"message": "overloaded"}})
    p, client, _ = provider([err] * 5)
    with pytest.raises(LLMError, match="503"):
        p.generate(model="m", system="", messages=[Message(role="user", text="x")])
    assert len(client.models.calls) == 5  # 1 + 4 retries


class Out(BaseModel):
    merchant: str
    amount: float


def test_structured_output_reasks_once_on_bad_json():
    p, client, _ = provider(
        [
            _raw([types.Part(text='{"merchant": "Netflix"}')]),
            _raw([types.Part(text='{"merchant": "Netflix", "amount": 649}')]),
        ]
    )
    r = p.generate(
        model="m", system="", messages=[Message(role="user", text="x")], response_schema=Out
    )
    assert Out.model_validate_json(r.text).amount == 649
    assert r.usage.input_tokens == 100  # both calls counted
    cfg = client.models.calls[0]["config"]
    assert cfg.response_mime_type == "application/json"
    assert "amount" in cfg.response_json_schema["properties"]
    reask = client.models.calls[1]["contents"][-1].parts[0].text
    assert "failed validation" in reask


def test_structured_output_fails_after_second_bad_json():
    bad = _raw([types.Part(text="not json")])
    p, _, _ = provider([bad, bad])
    with pytest.raises(LLMError, match="invalid JSON twice"):
        p.generate(
            model="m", system="", messages=[Message(role="user", text="x")], response_schema=Out
        )


def test_missing_key_is_clear_error():
    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        GeminiProvider(None)


def test_toolcall_without_id_gets_one():
    part = types.Part(function_call=types.FunctionCall(name="add", args={}))
    p, _, _ = provider([_raw([part])])
    r = p.generate(model="m", system="", messages=[Message(role="user", text="x")])
    assert isinstance(r.tool_calls[0], ToolCall) and r.tool_calls[0].id.startswith("call_")
