from leak.harness import PermissionPolicy
from tests.helpers import call, make_loop, resp


def test_needs_approval_is_queued_not_executed(make_budget, tmp_path):
    ran: list[str] = []
    script = [resp(calls=[call("send_greeting_draft", to="a@example.com")]), resp("done")]
    loop, provider = make_loop(script, make_budget(), tmp_path, ran=ran)
    result = loop.run("draft it")

    assert "send_greeting_draft" not in ran  # function never ran
    assert result.status == "completed"
    assert [a.tool for a in result.pending_actions] == ["send_greeting_draft"]
    assert result.pending_actions[0].args == {"to": "a@example.com"}
    assert loop.queue.pending() == result.pending_actions
    # The model was told honestly that nothing happened.
    tool_msg = provider.requests[1][-1]
    assert "NOT been executed" in tool_msg.tool_results[0].content


def test_forbidden_tool_hidden_and_refused(make_budget, tmp_path):
    ran: list[str] = []
    loop, provider = make_loop(
        [resp(calls=[call("rm_rf")]), resp("ok")], make_budget(), tmp_path, ran=ran
    )
    assert "rm_rf" not in [s.name for s in loop.registry.specs()]
    loop.run("x")
    assert ran == []
    r = provider.requests[1][-1].tool_results[0]
    assert r.is_error and "forbidden" in r.content


def test_policy_override_tightens_but_never_loosens(make_budget, tmp_path):
    ran: list[str] = []
    policy = PermissionPolicy({"add": "needs_approval", "rm_rf": "auto"})
    assert policy.decide("rm_rf", "forbidden") == "forbidden"
    loop, _ = make_loop(
        [resp(calls=[call("add", a=1, b=2)]), resp("ok")],
        make_budget(),
        tmp_path,
        ran=ran,
        policy=policy,
    )
    result = loop.run("x")
    assert ran == [] and result.pending_actions[0].tool == "add"


def test_auto_tools_run_and_bad_args_are_fed_back(make_budget, tmp_path):
    ran: list[str] = []
    script = [
        resp(calls=[call("add", "c1", a=2, b=3), call("add", "c2", a="two")]),
        resp(calls=[call("nope", "c3")]),
        resp("five"),
    ]
    loop, provider = make_loop(script, make_budget(), tmp_path, ran=ran)
    result = loop.run("x")
    assert result.status == "completed" and result.final_text == "five"
    ok, bad = provider.requests[1][-1].tool_results
    assert ok.content == 5 and not ok.is_error
    assert bad.is_error and "invalid arguments" in bad.content
    unknown = provider.requests[2][-1].tool_results[0]
    assert unknown.is_error and "unknown tool" in unknown.content
    assert ran == ["add"]
