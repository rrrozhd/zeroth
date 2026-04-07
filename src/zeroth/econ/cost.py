"""Cost estimation using litellm's cost_per_token lookup.

Converts token counts to USD cost via litellm's pricing database.
Returns Decimal("0") for unknown models instead of raising.
"""

from __future__ import annotations

from decimal import Decimal


class CostEstimator:
    """Estimates USD cost for LLM calls using litellm's pricing data."""

    def estimate(
        self,
        model_name: str,
        *,
        input_tokens: int,
        output_tokens: int,
    ) -> Decimal:
        """Return estimated USD cost as a Decimal.

        Uses litellm.cost_per_token() internally. Returns Decimal("0")
        if the model is unknown or any error occurs during lookup.
        """
        try:
            from litellm import cost_per_token

            prompt_cost, completion_cost = cost_per_token(
                model=model_name,
                prompt_tokens=input_tokens,
                completion_tokens=output_tokens,
            )
            return Decimal(str(prompt_cost + completion_cost))
        except Exception:
            return Decimal("0")
