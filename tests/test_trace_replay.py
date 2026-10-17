import json

from typer.testing import CliRunner

from leak.harness import ReplayProvider, load_trace, render_replay
from tests.helpers import call, make_loop, resp

SCRIPT = [
    resp("thinking", calls=[call("add", "c1", a=2, b=3), call("get_weather", "c2", city="Pune")]),
    resp(calls=[call("send_greeting_draft", "c3", to="x@example.com")]),
    resp("all done"),
]


def test_trace_written_and_replayable(make_budget, tmp_path):
    loop, _ = make_loop(SCRIPT, make_budget(), tmp_path)
    result = loop.run("hello")
    assert result.trace_path and result.trace_path.exists()

    lines = result.trace_path.read_text().splitlines()
    assert all(json.loads(line) for line in lines)  # valid JSONL
    events = load_trace(result.trace_path)
    types = [e.type for e in events]
    assert types[0] == "run_start" and types[-1] == "run_end"
    assert types.count("llm_request") == 3 == types.count("llm_response")
    assert "approval_queued" in types
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    # The exact payload the model saw is recorded.
    first_req = next(e for e in events if e.type == "llm_request")
    assert first_req.data["messages"][0]["text"] == "hello"

    text = render_replay(events)
    for needle in (
        "step 1",
        "step 2",
        "step 3",
        "add(",
        "← 5",
        "queued for approval",
        "completed",
        "all done",
        "₹",
    ):
        assert needle in text


def test_rerun_from_trace_is_deterministic(make_budget, tmp_path):
    loop, _ = make_loop(SCRIPT, make_budget(), tmp_path)
    original = loop.run("hello")
    events = load_trace(original.trace_path)

    from leak.demo_tools import build_toy_registry
    from leak.harness import AgentLoop

    rerun = AgentLoop(
        provider=ReplayProvider(events),
        model="test-model",
        registry=build_toy_registry(),
        budget=make_budget(),
        trace_dir=tmp_path,
        system="test",
    ).run("hello")
    assert rerun.status == original.status
    assert rerun.final_text == original.final_text
    assert [a.tool for a in rerun.pending_actions] == [a.tool for a in original.pending_actions]
    assert rerun.run_id != original.run_id


def test_cli_replay(tmp_path, monkeypatch):
    from leak.cli import app

    cfg = tmp_path / "config.toml"
    cfg.write_text(f"""
[llm]
extract_model = "test-model"
reason_model = "test-model"
[llm.prices."test-model"]
input = 1.0
output = 10.0
[paths]
data_dir = "{tmp_path / "data"}"
""")
    monkeypatch.setenv("LEAK_CONFIG", str(cfg))
    runner = CliRunner()
    out = runner.invoke(app, ["demo-harness"])
    assert out.exit_code == 0, out.output
    run_id = out.output.split("run_id:")[1].split()[0]
    replay = runner.invoke(app, ["trace", "replay", run_id])
    assert replay.exit_code == 0
    assert "step 3" in replay.output and "needs_approval" in replay.output
    assert runner.invoke(app, ["trace", "replay", "run_nope"]).exit_code == 1
