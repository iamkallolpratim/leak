"""Context: what goes into the prompt on each step.

The conversation grows by one assistant message and one batch of tool results
per step. Left alone, big tool outputs (ledger queries, evidence excerpts)
would eat the context window and the budget. `compact()` applies the
simplest policy that works: keep the most recent tool results verbatim and
replace older, large ones with a short stub telling the model how to get the
data back. Phase 4 adds LLM summarization on top; the interface stays.
"""

from __future__ import annotations

import json

from leak.llm.provider import Message, ToolResult


class Context:
    def __init__(self, system: str, *, keep_recent: int = 4, max_result_chars: int = 2000) -> None:
        self.system = system
        self.messages: list[Message] = []
        self.keep_recent = keep_recent  # tool-result messages kept verbatim
        self.max_result_chars = max_result_chars

    def add_user(self, text: str) -> None:
        self.messages.append(Message(role="user", text=text))

    def add(self, message: Message) -> None:
        self.messages.append(message)

    def compact(self) -> int:
        """Stub out large tool results older than the last `keep_recent`
        tool messages. Returns how many results were compacted."""
        tool_msgs = [m for m in self.messages if m.role == "tool"]
        compacted = 0
        for msg in tool_msgs[: -self.keep_recent or None]:
            new_results: list[ToolResult] = []
            for r in msg.tool_results:
                size = len(json.dumps(r.content, ensure_ascii=False, default=str))
                if size > self.max_result_chars:
                    r = r.model_copy(
                        update={
                            "content": f"[compacted: {size} chars from {r.name}; "
                            "call the tool again if you need it]"
                        }
                    )
                    compacted += 1
                new_results.append(r)
            msg.tool_results = new_results
        return compacted
