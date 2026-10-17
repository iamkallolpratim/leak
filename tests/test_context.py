from leak.harness.context import Context
from leak.llm.provider import Message, ToolResult


def _tool_msg(i: int, size: int) -> Message:
    return Message(
        role="tool", tool_results=[ToolResult(call_id=f"c{i}", name="q", content="x" * size)]
    )


def test_compacts_old_large_results_only():
    ctx = Context("sys", keep_recent=2, max_result_chars=100)
    for i in range(4):
        ctx.add(_tool_msg(i, 500))
    ctx.add(_tool_msg(9, 10))
    assert ctx.compact() == 3  # msgs 0-2 compacted; last 2 tool msgs kept verbatim
    contents = [m.tool_results[0].content for m in ctx.messages]
    assert all(c.startswith("[compacted") for c in contents[:3])
    assert contents[3] == "x" * 500 and contents[4] == "x" * 10
