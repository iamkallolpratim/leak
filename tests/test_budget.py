from decimal import Decimal

import pytest

from leak.harness import BudgetExceeded
from leak.llm.provider import Usage
from tests.helpers import MODEL, call, make_loop, resp


def test_step_budget_stops_runaway_loop(make_budget, tmp_path):
    # A model that never stops calling tools.
    loop, provider = make_loop(
        lambda msgs: resp(calls=[call("add", a=1, b=1)]), make_budget(max_steps=3), tmp_path
    )
    result = loop.run("go")
    assert result.status == "budget_exceeded"
    assert "step budget" in (result.reason or "")
    assert len(provider.requests) == 3  # never made a 4th LLM call


def test_cost_budget_stops_loop(make_budget, tmp_path):
    # Each call: 100k in * $1/M + 10k out * $10/M = $0.10 + $0.10 = $0.20
    expensive = resp(calls=[call("add", a=1, b=1)], inp=100_000, out=10_000)
    loop, provider = make_loop(lambda msgs: expensive, make_budget(max_usd="0.50"), tmp_path)
    result = loop.run("go")
    assert result.status == "budget_exceeded"
    assert "cost budget" in (result.reason or "")
    assert len(provider.requests) == 3  # 0.20, 0.40, 0.60 > 0.50 -> stop
    assert loop.budget.spent_usd == Decimal("0.60")


def test_cost_math_with_cache(make_budget):
    b = make_budget()
    # 1M input of which 400k cached, 100k output:
    # 600k*$1 + 400k*$0.10 + 100k*$10, per 1M = 0.6 + 0.04 + 1.0
    cost = b.cost_of(
        Usage(input_tokens=1_000_000, cached_tokens=400_000, output_tokens=100_000), MODEL
    )
    assert cost == Decimal("1.64")
    assert b.to_inr(cost) == Decimal("144.32")


def test_unpriced_model_is_refused(make_budget):
    with pytest.raises(BudgetExceeded, match="no price"):
        make_budget().cost_of(Usage(input_tokens=1), "mystery-model")
