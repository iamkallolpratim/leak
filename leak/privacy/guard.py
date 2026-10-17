"""Egress guard: the last checkpoint before text leaves the machine.

Ingestion already stores redacted text and tools return redacted text, so in
the happy path this changes nothing. It exists because "every tool author
remembered to redact" is not a safety property. The guard:

1. redacts every string in the outgoing payload (system prompt, messages,
   tool results, tool-call args), then
2. re-scans the serialized payload and *refuses to send* if anything still
   looks like PII (fail closed).

Use it two ways:
* `AgentLoop(egress=guard.filter)` -- the loop traces the filtered payload,
  so the trace shows exactly what the LLM saw.
* `GuardedProvider(inner, guard)` -- for one-off LLM calls outside the loop.

The other direction matters too: text the *model* wrote (finding merchant
names, explanations) is untrusted -- it read your emails, and an email can
carry a prompt injection. `vet_outbound()` checks it before it lands in a
Gmail draft or a calendar file, and before a finding is accepted at all.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from leak.llm.provider import LLMError, LLMProvider, LLMResponse, Message, ToolSpec
from leak.privacy.redact import Redactor, find_pii


class PrivacyViolation(LLMError):
    """Raised instead of sending a payload that still contains PII."""


class EgressGuard:
    def __init__(self, redactor: Redactor) -> None:
        self.redactor = redactor

    def scrub(self, value: Any) -> Any:
        """Redact every string inside any JSON-like value."""
        if isinstance(value, str):
            return self.redactor.redact(value)
        if isinstance(value, list):
            return [self.scrub(v) for v in value]
        if isinstance(value, dict):
            return {k: self.scrub(v) for k, v in value.items()}
        return value

    def filter(self, system: str, messages: list[Message]) -> tuple[str, list[Message]]:
        clean_system = self.redactor.redact(system)
        clean: list[Message] = []
        for m in messages:
            clean.append(
                m.model_copy(
                    update={
                        "text": self.redactor.redact(m.text),
                        "tool_calls": [
                            c.model_copy(update={"args": self.scrub(c.args)}) for c in m.tool_calls
                        ],
                        "tool_results": [
                            r.model_copy(update={"content": self.scrub(r.content)})
                            for r in m.tool_results
                        ],
                    }
                )
            )
        self.verify(clean_system, clean)
        return clean_system, clean

    def verify(self, system: str, messages: list[Message]) -> None:
        # Scan values one by one (not a JSON dump: escaping would hide things).
        for text in _strings([system, [m.model_dump(mode="json") for m in messages]]):
            leaks = self.redactor.leaks(text)
            if leaks:
                kinds = sorted({s.kind for s in leaks})
                raise PrivacyViolation(f"refusing to send payload: unredacted {kinds}")


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return [json.dumps(value)] if value is not None else []


class GuardedProvider:
    """Wrap any provider so nothing reaches it unredacted."""

    def __init__(self, inner: LLMProvider, guard: EgressGuard) -> None:
        self.inner = inner
        self.guard = guard
        self.name = f"guarded:{inner.name}"

    def generate(
        self,
        *,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        response_schema: type[BaseModel] | None = None,
    ) -> LLMResponse:
        system, messages = self.guard.filter(system, messages)
        return self.inner.generate(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            response_schema=response_schema,
        )


# --- the other direction: model-generated text ------------------------------------

_LINK = re.compile(r"(?i)\b(?:https?://|www\.)\S+")


def vet_outbound(
    texts: Mapping[str, str], *, untrusted: Mapping[str, str] | None = None
) -> list[str]:
    """Problems with text about to leave Leak through an action (a Gmail draft,
    an .ics file) or to be stored as a finding. Empty list = safe.

    `texts` are scanned for PII. `untrusted` are fields the model controls;
    they are also scanned and must not carry links or line breaks. Messages
    name the field and the *kind* of data, never the value itself, so they are
    safe to store and show.
    """
    problems: list[str] = []
    for label, text in {**texts, **(untrusted or {})}.items():
        kinds = sorted({s.kind for s in find_pii(text)})
        if kinds:
            problems.append(f"{label} contains what looks like {'/'.join(kinds)}")
    for label, text in (untrusted or {}).items():
        if _LINK.search(text):
            problems.append(f"{label} contains a link")
        if label == "merchant" and ("\n" in text or "\r" in text):
            problems.append("merchant spans several lines")
    return list(dict.fromkeys(problems))  # dedupe, keep order
