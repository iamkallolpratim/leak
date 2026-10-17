"""Configuration: TOML file + .env for secrets.

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

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from leak.harness.budget import Budget, ModelPrice
from leak.harness.permissions import Permission
from leak.ingest.gmail import DEFAULT_QUERY
from leak.privacy.redact import PrivacyProfile

_PKG_DIR = Path(__file__).resolve().parent


class LLMConfig(BaseModel):
    provider: str = "gemini"
    extract_model: str
    reason_model: str
    prices: dict[str, ModelPrice] = Field(default_factory=dict)


class BudgetConfig(BaseModel):
    max_steps: int = 25
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

    def all_passwords(self) -> list[str]:
        env = os.environ.get("LEAK_PDF_PASSWORDS", "")
        return [*self.passwords, *(p.strip() for p in env.split(",") if p.strip())]


class AgentConfig(BaseModel):
    max_steps: int = 30
    max_usd: Decimal = Decimal("1.00")


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
    def gemini_api_key(self) -> str | None:
        return os.environ.get("GEMINI_API_KEY") or None

    def make_budget(self) -> Budget:
        return Budget(
            max_steps=self.budget.max_steps,
            max_usd=self.budget.max_usd,
            prices=self.llm.prices,
            usd_inr=self.budget.usd_inr,
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
    load_dotenv()
    path = path or _find_config()
    with path.open("rb") as f:
        data = tomllib.load(f)
    if overlay:
        with overlay.open("rb") as f:
            data.update(tomllib.load(f))
    return Settings.model_validate({**data, "source": path})
