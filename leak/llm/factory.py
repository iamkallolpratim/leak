"""The one place that decides whether an LLM is used at all.

Gemini is optional. With no `GEMINI_API_KEY`, or `[llm] provider = "none"`,
this returns None and every caller falls back to its local-only path. When it
does return a provider, callers still wrap it in the egress guard: only masked
text is ever sent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from leak.llm.provider import LLMProvider

if TYPE_CHECKING:
    from leak.config import Settings


def make_provider(settings: Settings) -> LLMProvider | None:
    if settings.llm.provider == "none" or not settings.gemini_api_key:
        return None
    from leak.llm.gemini import GeminiProvider  # lazy: keeps google-genai off the local path

    return GeminiProvider(settings.gemini_api_key)
