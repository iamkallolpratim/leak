"""Budgets: hard ceilings on steps and money for one run.

An agent loop with no ceiling is a bug waiting for a credit-card statement.
We check two things:

* steps -- checked *before* each LLM call ("may I take another step?")
* cost  -- checked *after* each LLM call, once we know the real token usage

Costs are computed from the provider's reported token counts and a per-model
price table from config (USD per 1M tokens). Shown in USD and INR because the
first users think in rupees. The INR rate is a config constant for now.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel

from leak.llm.provider import Usage

MILLION = Decimal(1_000_000)


class ModelPrice(BaseModel):
    input: Decimal  # USD per 1M uncached input tokens
    output: Decimal  # USD per 1M output tokens (thinking included)
    cached_input: Decimal = Decimal(0)  # USD per 1M cached input tokens


class BudgetExceeded(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Budget:
    def __init__(
        self,
        *,
        max_steps: int,
        max_usd: Decimal,
        prices: dict[str, ModelPrice],
        usd_inr: Decimal = Decimal("84"),
    ) -> None:
        self.max_steps = max_steps
        self.max_usd = max_usd
        self.prices = prices
        self.usd_inr = usd_inr
        self.steps = 0
        self.spent_usd = Decimal(0)
        self.input_tokens = 0
        self.output_tokens = 0

    # -- steps --------------------------------------------------------------
    def start_step(self) -> None:
        """Call before every LLM request."""
        if self.steps >= self.max_steps:
            raise BudgetExceeded(f"step budget exhausted ({self.max_steps} steps)")
        self.steps += 1

    # -- money --------------------------------------------------------------
    def cost_of(self, usage: Usage, model: str) -> Decimal:
        price = self.prices.get(model)
        if price is None:
            # Refuse to guess: an unpriced model would silently bypass the budget.
            raise BudgetExceeded(f"no price configured for model '{model}'")
        uncached = max(usage.input_tokens - usage.cached_tokens, 0)
        return (
            Decimal(uncached) * price.input
            + Decimal(usage.cached_tokens) * price.cached_input
            + Decimal(usage.output_tokens) * price.output
        ) / MILLION

    def charge(self, usage: Usage, model: str) -> Decimal:
        """Record a call's cost; raise if we are now over budget.

        The call already happened, so we still record it -- the trace must
        show what was actually spent, including the call that tipped us over.
        """
        cost = self.cost_of(usage, model)
        self.spent_usd += cost
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        if self.spent_usd > self.max_usd:
            raise BudgetExceeded(
                f"cost budget exceeded: ${self.spent_usd:.4f} > ${self.max_usd} "
                f"(₹{self.to_inr(self.spent_usd):.2f})"
            )
        return cost

    def to_inr(self, usd: Decimal) -> Decimal:
        return usd * self.usd_inr

    def snapshot(self) -> dict[str, str | int]:
        return {
            "steps": self.steps,
            "max_steps": self.max_steps,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "spent_usd": f"{self.spent_usd:.6f}",
            "spent_inr": f"{self.to_inr(self.spent_usd):.4f}",
            "max_usd": str(self.max_usd),
        }
