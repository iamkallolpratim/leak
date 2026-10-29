"""The one place that decides whether an LLM is used at all.

OpenRouter is the only remote gateway, and it is optional. With no key for
this profile, or `[llm] provider = "none"`, this returns None and every caller
falls back to its local-only path. When it does return a provider, callers
still put it behind the egress guard: only masked text is ever sent, and the
provider itself refuses to send without that gate.

A new provider (and HTTP client) is built for every call, from the key as it
is *now*. Nothing is cached, so a replaced or removed key never lingers and
two profiles can't share a client.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx2 as httpx

from leak.llm.credentials import KeyStore, resolve_key
from leak.llm.openrouter import OpenRouterProvider
from leak.llm.provider import LLMProvider

if TYPE_CHECKING:
    from leak.config import Settings


class ModelConfigError(ValueError):
    pass


def check_model_ids(settings: Settings) -> None:
    for role, model in (("extract", settings.llm.extract_model),
                        ("reason", settings.llm.reason_model)):  # fmt: skip
        if "/" not in model:
            raise ModelConfigError(
                f"[llm] {role}_model = '{model}' is not an OpenRouter model id. They look "
                "like 'vendor/model', e.g. 'google/gemini-3.5-flash-lite'. "
                "Run `leak llm models` to check your choice."
            )


def make_provider(
    settings: Settings,
    *,
    store: KeyStore | None = None,
    transport: httpx.BaseTransport | None = None,
) -> LLMProvider | None:
    if settings.llm.provider == "none":
        return None
    resolved = resolve_key(
        store or KeyStore(settings.profile_id),
        env_file_key_refused=settings.env_file_key_refused,
    )
    if resolved is None:
        return None
    check_model_ids(settings)
    return OpenRouterProvider(
        resolved.key,
        timeout=settings.llm.timeout_s,
        max_retries=settings.llm.max_retries,
        transport=transport,
    )
