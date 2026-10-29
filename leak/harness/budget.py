"""Budgets: hard ceilings on steps and money for one run.

An agent loop with no ceiling is a bug waiting for a credit-card statement.
We check two things:

* steps   -- checked *before* each LLM call ("may I take another step?")
* retries -- every HTTP retry or JSON re-ask is checked *before* it is sent,
             against its own per-run ceiling, so a flaky upstream can't loop
* cost    -- checked *before* each request (is anything left?) and *after*
             it, once we know what it cost

Cost per call: what the gateway reports it billed (OpenRouter `usage.cost`)
when it reports one; otherwise an estimate from the token counts and a
per-model price table from config (USD per 1M tokens). No reported cost and no
price means we stop rather than count the call as free. Either way these are
Leak's *local* numbers: the bill is whatever your OpenRouter account says.

Shown in USD and INR because the first users think in rupees. The INR rate is
a config constant for now.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel

from leak.llm.provider import Message, Usage

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
        max_retries: int = 10,
    ) -> None:
        self.max_steps = max_steps
        self.max_usd = max_usd
        self.prices = prices
        self.usd_inr = usd_inr
        self.max_retries = max_retries
        self.steps = 0
        self.retries = 0
        self.spent_usd = Decimal(0)
        self.reported_usd = Decimal(0)  # part of spent_usd the gateway reported
        self.estimated_usd = Decimal(0)  # part of spent_usd we priced ourselves
        self.input_tokens = 0
        self.output_tokens = 0

    # -- steps --------------------------------------------------------------
    def start_step(self) -> None:
        """Call before every LLM request."""
        if self.steps >= self.max_steps:
            raise BudgetExceeded(f"step budget exhausted ({self.max_steps} steps)")
        self.check_funds()
        self.steps += 1

    def start_retry(self) -> None:
        """Call before every retry / re-ask of a request (they cost too)."""
        if self.retries >= self.max_retries:
            raise BudgetExceeded(f"retry budget exhausted ({self.max_retries} retries)")
        self.check_funds()
        self.retries += 1

    # -- money --------------------------------------------------------------
    def check_funds(self) -> None:
        """Call before any request: nothing is sent once the money is gone."""
        if self.spent_usd >= self.max_usd:
            raise BudgetExceeded(
                f"cost budget exhausted: ${self.spent_usd:.4f} of ${self.max_usd} spent"
            )

    def cost_of(self, usage: Usage, model: str) -> Decimal:
        if usage.cost_usd is not None:
            return max(usage.cost_usd, Decimal(0))  # what the gateway says it billed
        price = self.prices.get(model)
        if price is None:
            # Refuse to guess: an unpriced model would silently bypass the budget.
            raise BudgetExceeded(
                f"no price configured for model '{model}' and no cost reported: "
                "refusing to count the call as free (add it to [llm.prices])"
            )
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
        if usage.cost_usd is not None:
            self.reported_usd += cost
        else:
            self.estimated_usd += cost
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        if self.spent_usd > self.max_usd:
            raise BudgetExceeded(
                f"cost budget exceeded: ${self.spent_usd:.4f} > ${self.max_usd} "
                f"(₹{self.to_inr(self.spent_usd):.2f})"
            )
        return cost

    def gate(self, attempt: int, system: str, messages: list[Message]) -> tuple[str, list[Message]]:
        """A `before_request` hook for one-off calls: funds before the first
        attempt, the retry ceiling before every later one."""
        if attempt == 0:
            self.check_funds()
        else:
            self.start_retry()
        return system, messages

    def to_inr(self, usd: Decimal) -> Decimal:
        return usd * self.usd_inr

    def snapshot(self) -> dict[str, str | int]:
        return {
            "steps": self.steps,
            "max_steps": self.max_steps,
            "retries": self.retries,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "spent_usd": f"{self.spent_usd:.6f}",
            "spent_inr": f"{self.to_inr(self.spent_usd):.4f}",
            "reported_usd": f"{self.reported_usd:.6f}",
            "estimated_usd": f"{self.estimated_usd:.6f}",
            "max_usd": str(self.max_usd),
        }
