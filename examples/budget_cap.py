"""Cap a run's cost budget — runnable example for docs/how-to/cookbook/budget-cap.md.

Uses the offline ``CostEstimator`` to convert token usage into a USD cost
for a proposed LLM call, then enforces a local per-run budget cap before
dispatching. Demonstrates the same two-step "estimate then gate" pattern
that the production ``BudgetEnforcer`` uses against the Regulus backend,
but runs fully in-process so no external service is needed.
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal


def _estimate_call_cost(model_name: str, input_tokens: int, output_tokens: int) -> Decimal:
    from zeroth.core.econ import CostEstimator

    estimator = CostEstimator()
    return estimator.estimate(
        model_name,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _dispatch_or_block(
    *,
    model_name: str,
    input_tokens: int,
    output_tokens: int,
    spend_so_far_usd: Decimal,
    budget_cap_usd: Decimal,
) -> bool:
    cost = _estimate_call_cost(model_name, input_tokens, output_tokens)
    projected = spend_so_far_usd + cost
    print(
        f"model={model_name} in={input_tokens} out={output_tokens} "
        f"estimated=${cost:.6f} projected=${projected:.6f} cap=${budget_cap_usd:.6f}"
    )
    if projected > budget_cap_usd:
        print("BLOCK: projected spend exceeds per-run budget cap")
        return False
    print("ALLOW: call fits inside budget")
    return True


def main() -> int:
    required_env: list[str] = []
    missing = [k for k in required_env if not os.environ.get(k)]
    if missing:
        print(f"SKIP: missing env vars: {', '.join(missing)}", file=sys.stderr)
        return 0

    # Simulate a run that has already spent $0.0009. The per-run cap is
    # $0.001, so a 500-in / 500-out call on gpt-4o-mini should be blocked.
    budget_cap = Decimal("0.001")
    spend_so_far = Decimal("0.0009")

    # First call: a tiny one that should fit.
    assert _dispatch_or_block(
        model_name="openai/gpt-4o-mini",
        input_tokens=20,
        output_tokens=20,
        spend_so_far_usd=spend_so_far,
        budget_cap_usd=budget_cap,
    )

    # Second call: large enough to blow the cap.
    blocked = _dispatch_or_block(
        model_name="openai/gpt-4o-mini",
        input_tokens=5000,
        output_tokens=5000,
        spend_so_far_usd=spend_so_far,
        budget_cap_usd=budget_cap,
    )
    assert blocked is False, "expected the second call to be blocked by the budget cap"
    print("budget-cap demo OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
