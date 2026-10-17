import os
from decimal import Decimal
from pathlib import Path

import pytest

from leak.harness import Budget, ModelPrice

# Never read the developer's own config.toml / .env settings during tests: a
# real config may enable Gmail actions, which would open an OAuth browser flow.
# Set at import time so module-scoped fixtures see it too.
os.environ["LEAK_CONFIG"] = str(Path(__file__).parents[1] / "config.example.toml")
os.environ["LEAK_PDF_PASSWORDS"] = ""

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
