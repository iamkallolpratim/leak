"""Configuration: TOML file, plus a few environment overrides.

The OpenRouter API key is *not* configuration: it lives in the OS keychain
(or OPENROUTER_API_KEY in the real environment); see leak/llm/credentials.py.
`.env` may still hold non-key settings (LEAK_PDF_PASSWORDS, model overrides).

Lookup order for the config file:
  1. $LEAK_CONFIG
  2. ./config.toml
  3. config.example.toml (repo root, or the copy bundled in the wheel)
"""

from __future__ import annotations

import os
import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

from leak.harness.budget import Budget, ModelPrice
from leak.harness.permissions import Permission
from leak.ingest.gmail import DEFAULT_QUERY
from leak.ingest.ocr import OcrConfig
from leak.llm.credentials import ENV_VAR as KEY_ENV_VAR
from leak.llm.credentials import profile_id
from leak.privacy.redact import PrivacyProfile

_PKG_DIR = Path(__file__).resolve().parent


MODEL_ENV = {"extract_model": "OPENROUTER_MODEL_EXTRACT", "reason_model": "OPENROUTER_MODEL_REASON"}


class LLMConfig(BaseModel):
    # "openrouter": used only when this profile has a key, and only ever sent masked text.
    # "none" keeps everything on this machine even when a key exists: bank plugins,
    # OCR, SMS rules and the detectors still run; LLM steps are skipped.
    provider: Literal["openrouter", "none"] = "openrouter"
    extract_model: str  # OpenRouter ids, "vendor/model"; env OPENROUTER_MODEL_EXTRACT wins
    reason_model: str  # env OPENROUTER_MODEL_REASON wins
    timeout_s: float = Field(120.0, gt=0, le=600)  # per HTTP attempt
    max_retries: int = Field(4, ge=0, le=8)  # per call, for 408/429/5xx/network only
    prices: dict[str, ModelPrice] = Field(default_factory=dict)

    @field_validator("provider", mode="before")
    @classmethod
    def _no_gemini(cls, v: object) -> object:
        if v == "gemini":
            raise ValueError(
                'provider "gemini" was removed: Leak now uses OpenRouter with your own key. '
                'Set provider = "openrouter" (or "none"), prefix model ids with "google/", '
                "and run `leak llm key set`. See README > LLM setup."
            )
        return v


class BudgetConfig(BaseModel):
    max_steps: int = 25
    max_retries: int = 10  # HTTP retries + JSON re-asks per run, on top of steps
    max_usd: Decimal = Decimal("0.50")
    usd_inr: Decimal = Decimal("88")


class PathsConfig(BaseModel):
    data_dir: Path = Path(".leak")


class PrivacyConfig(BaseModel):
    """Your own identifiers. Always masked, wherever they appear."""

    my_names: list[str] = Field(default_factory=list)
    my_emails: list[str] = Field(default_factory=list)
    my_address_fragments: list[str] = Field(default_factory=list)

    def profile(self) -> PrivacyProfile:
        return PrivacyProfile(
            names=self.my_names, emails=self.my_emails, address_fragments=self.my_address_fragments
        )


class GmailConfig(BaseModel):
    client_secret: Path = Path("~/.config/leak/client_secret.json")
    query: str = DEFAULT_QUERY
    max_messages: int = 2000
    enable_actions: bool = False  # adds gmail.compose scope (drafts only)


class StatementsConfig(BaseModel):
    dir: Path | None = None
    # Prefer LEAK_PDF_PASSWORDS="pw1,pw2" in .env over putting them here.
    passwords: list[str] = Field(default_factory=list)
    llm_fallback: bool = True
    ocr: OcrConfig = Field(default_factory=OcrConfig)

    def all_passwords(self) -> list[str]:
        env = os.environ.get("LEAK_PDF_PASSWORDS", "")
        return [*self.passwords, *(p.strip() for p in env.split(",") if p.strip())]


class AgentConfig(BaseModel):
    max_steps: int = 30
    max_usd: Decimal = Decimal("1.00")
    max_retries: int = 10


class SmsConfig(BaseModel):
    paths: list[Path] = Field(default_factory=list)


class Settings(BaseModel):
    llm: LLMConfig
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    permissions: dict[str, Permission] = Field(default_factory=dict)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    gmail: GmailConfig = Field(default_factory=GmailConfig)
    statements: StatementsConfig = Field(default_factory=StatementsConfig)
    sms: SmsConfig = Field(default_factory=SmsConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    db_name: str = "leak.db"
    source: Path | None = None
    # True when OPENROUTER_API_KEY came only from a .env file: we refuse to use it.
    env_file_key_refused: bool = False

    @property
    def trace_dir(self) -> Path:
        """Traces hold prompts built from ledger data, so demo and real runs must
        never share a folder (the dashboard lists every trace in it)."""
        stem = Path(self.db_name).stem
        return self.paths.data_dir / ("traces" if stem == "leak" else f"{stem}_traces")

    @property
    def db_path(self) -> Path:
        return self.paths.data_dir / self.db_name

    @property
    def vault_path(self) -> Path:
        return self.paths.data_dir / self.db_name.replace(".db", "_vault.db")

    @property
    def gmail_token_path(self) -> Path:
        return self.paths.data_dir / "gmail_token.json"

    @property
    def profile_id(self) -> str:
        """One keychain entry per data dir: profiles never share a key."""
        return profile_id(self.paths.data_dir)

    def make_budget(self) -> Budget:
        return Budget(
            max_steps=self.budget.max_steps,
            max_usd=self.budget.max_usd,
            prices=self.llm.prices,
            usd_inr=self.budget.usd_inr,
            max_retries=self.budget.max_retries,
        )


def _find_config() -> Path:
    candidates = [Path(p) for p in [os.environ.get("LEAK_CONFIG")] if p] + [
        Path("config.toml"),
        _PKG_DIR.parent / "config.example.toml",
        _PKG_DIR / "config.example.toml",
    ]
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError("no config found; copy config.example.toml to config.toml")


def load_settings(path: Path | None = None, overlay: Path | None = None) -> Settings:
    """Load config; `overlay` (e.g. the demo's demo.toml) replaces whole sections."""
    refused = _load_dotenv()
    path = path or _find_config()
    with path.open("rb") as f:
        data = tomllib.load(f)
    if overlay:
        with overlay.open("rb") as f:
            data.update(tomllib.load(f))
    llm = dict(data.get("llm", {}))
    for field, env in MODEL_ENV.items():
        if os.environ.get(env, "").strip():
            llm[field] = os.environ[env].strip()
    return Settings.model_validate(
        {**data, "llm": llm, "source": path, "env_file_key_refused": refused}
    )


def _load_dotenv() -> bool:
    """Load .env, but never let it supply the API key (a plaintext file).

    Returns True if .env tried to: the key is dropped from the environment and
    resolving a key will explain why, instead of silently using or ignoring it.
    """
    preset = KEY_ENV_VAR in os.environ
    load_dotenv()
    if not preset and KEY_ENV_VAR in os.environ:
        return bool(os.environ.pop(KEY_ENV_VAR).strip())
    return False
