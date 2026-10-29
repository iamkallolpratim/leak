import os
from decimal import Decimal
from pathlib import Path

import keyring
import pytest

import leak.config
from leak.harness import Budget, ModelPrice
from tests.helpers import MemoryKeyring

# Never read the developer's own config.toml / .env settings during tests: a
# real config may enable Gmail actions, which would open an OAuth browser flow.
# Set at import time so module-scoped fixtures see it too.
os.environ["LEAK_CONFIG"] = str(Path(__file__).parents[1] / "config.example.toml")
os.environ["LEAK_PDF_PASSWORDS"] = ""
# ...and never a real OpenRouter key: not from the environment, not from the
# developer's .env, not from their OS keychain. Tests can't reach the network.
os.environ.pop("OPENROUTER_API_KEY", None)
os.environ.pop("OPENROUTER_MODEL_EXTRACT", None)
os.environ.pop("OPENROUTER_MODEL_REASON", None)
leak.config.load_dotenv = lambda *a, **k: False  # type: ignore[assignment]
keyring.set_keyring(MemoryKeyring())


@pytest.fixture(autouse=True)
def _fresh_keychain():
    """Every test starts with an empty in-memory keychain."""
    keyring.set_keyring(MemoryKeyring())
    yield


MODEL = "test-model"
PRICES = {
    MODEL: ModelPrice(input=Decimal("1.00"), output=Decimal("10.00"), cached_input=Decimal("0.10"))
}


@pytest.fixture
def make_budget():
    def _make(max_steps: int = 10, max_usd: str = "1.00") -> Budget:
        return Budget(
            max_steps=max_steps, max_usd=Decimal(max_usd), prices=PRICES, usd_inr=Decimal("88")
        )

    return _make
