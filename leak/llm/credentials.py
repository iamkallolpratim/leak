"""BYOK: the user's own OpenRouter key, kept per profile.

Leak ships no key and has no fallback key. Inference is billed to whoever owns
the key, so it is always yours.

A *profile* is a data directory (`[paths] data_dir`, default `.leak`). Each one
has its own keychain entry, so two profiles never see each other's key:

    service "leak-openrouter", account "profile:<sha256(abs data_dir)[:16]>"

Where a key may come from, in this order:

1. `OPENROUTER_API_KEY` in the real process environment: CI, containers and
   headless boxes, where a secret store injects it per process.
2. The OS keychain via `keyring` (macOS Keychain, Windows Credential Locker,
   Secret Service / KWallet on Linux). `leak llm key set` puts it there.

Never: config.toml, SQLite, traces, logs, or a `.env` file. A key found only in
`.env` is refused with an explanation rather than used, and a plaintext or
"fail" keyring backend is refused rather than written to.

Nothing here is cached or global: every lookup asks the keychain again, so a
replaced or removed key takes effect on the next provider that is built.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import keyring
import keyring.errors
from keyring.backend import KeyringBackend

from leak.llm.openrouter import mask_key

SERVICE = "leak-openrouter"
ENV_VAR = "OPENROUTER_API_KEY"

KeySource = Literal["env", "keychain"]

HEADLESS_HINT = (
    "For CI, containers or headless machines, provide the key per process with the "
    f"{ENV_VAR} environment variable from your secret store (e.g. a CI secret, a "
    "container secret, or `read -s OPENROUTER_API_KEY && export OPENROUTER_API_KEY`)."
)


class CredentialError(RuntimeError):
    """A key problem the user must fix. Messages never contain the key."""


class SecureStorageUnavailable(CredentialError):
    pass


def profile_id(data_dir: Path) -> str:
    resolved = str(data_dir.expanduser().resolve())
    return hashlib.sha256(resolved.encode()).hexdigest()[:16]


def check_key_format(key: str) -> str:
    key = key.strip()
    if not key:
        raise CredentialError("empty key")
    if any(c.isspace() for c in key) or not key.isascii() or not key.isprintable():
        raise CredentialError("the key contains spaces or unexpected characters")
    if not key.startswith("sk-or-") or not 20 <= len(key) <= 256:
        raise CredentialError(
            "that does not look like an OpenRouter key (they start with 'sk-or-'); "
            "create one at https://openrouter.ai/settings/keys"
        )
    return key


def backend_name(backend: KeyringBackend) -> str:
    return f"{type(backend).__module__}.{type(backend).__name__}"


def is_secure(backend: KeyringBackend) -> bool:
    mod, name = type(backend).__module__, type(backend).__name__
    if mod in ("keyring.backends.fail", "keyring.backends.null") or "Plaintext" in name:
        return False
    if mod == "keyring.backends.chainer":
        chain = list(getattr(backend, "backends", []))
        return bool(chain) and is_secure(chain[0])  # writes go to the first one
    return True


@dataclass(frozen=True)
class ResolvedKey:
    key: str = field(repr=False)
    source: KeySource
    profile: str

    @property
    def masked(self) -> str:
        return mask_key(self.key)


class KeyStore:
    """The keychain entry for one profile. `backend` is injectable for tests."""

    def __init__(self, profile: str, backend: KeyringBackend | None = None) -> None:
        self.profile = profile
        self.account = f"profile:{profile}"
        self._backend = backend

    @classmethod
    def for_data_dir(cls, data_dir: Path, backend: KeyringBackend | None = None) -> KeyStore:
        return cls(profile_id(data_dir), backend)

    @property
    def backend(self) -> KeyringBackend:
        return self._backend or keyring.get_keyring()

    def _secure_backend(self) -> KeyringBackend:
        b = self.backend
        if not is_secure(b):
            raise SecureStorageUnavailable(
                f"no secure OS keychain is available (keyring backend: {backend_name(b)}). "
                "Leak will not store your key in a plaintext file. Install or unlock a "
                "keychain (on Linux: gnome-keyring/Secret Service or KWallet), or skip "
                f"storage entirely. {HEADLESS_HINT}"
            )
        return b

    def get(self) -> str | None:
        try:
            return self._secure_backend().get_password(SERVICE, self.account) or None
        except keyring.errors.KeyringError as e:
            raise SecureStorageUnavailable(
                f"could not read the keychain: {type(e).__name__}"
            ) from None

    def set(self, key: str) -> None:
        key = check_key_format(key)
        try:
            self._secure_backend().set_password(SERVICE, self.account, key)
        except keyring.errors.KeyringError as e:
            raise SecureStorageUnavailable(
                f"could not write to the keychain: {type(e).__name__}. {HEADLESS_HINT}"
            ) from None

    def delete(self) -> bool:
        b = self._secure_backend()
        try:
            b.delete_password(SERVICE, self.account)
        except keyring.errors.PasswordDeleteError:
            return False
        except keyring.errors.KeyringError as e:
            raise SecureStorageUnavailable(
                f"could not edit the keychain: {type(e).__name__}"
            ) from None
        return True


def resolve_key(store: KeyStore, *, env_file_key_refused: bool = False) -> ResolvedKey | None:
    """The key to use for this profile, or None when there is none.

    Raises CredentialError when the only key on offer is one we refuse to use.
    A missing or unusable keychain is "no key" here (local-only mode still
    works); `leak llm status` explains it.
    """
    env = os.environ.get(ENV_VAR, "").strip()
    if env:
        return ResolvedKey(check_key_format(env), "env", store.profile)
    try:
        stored = store.get()
    except SecureStorageUnavailable:
        stored = None
    if stored:
        return ResolvedKey(stored, "keychain", store.profile)
    if env_file_key_refused:
        raise CredentialError(
            f"{ENV_VAR} is set in a .env file. Leak does not read API keys from plaintext "
            "files: remove it from .env and run `leak llm key set` (OS keychain). " + HEADLESS_HINT
        )
    return None
