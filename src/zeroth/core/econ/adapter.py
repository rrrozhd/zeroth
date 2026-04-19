"""InstrumentedProviderAdapter -- cost-tracking decorator for any ProviderAdapter.

Wraps any ProviderAdapter to:
1. Measure wall-clock latency of the LLM call
2. Estimate USD cost via CostEstimator (litellm pricing)
3. Emit a Regulus ExecutionEvent via RegulusClient (fire-and-forget)
4. Return enriched ProviderResponse with cost_usd and cost_event_id
"""

from __future__ import annotations

from decimal import Decimal
from time import perf_counter

from econ_instrumentation import ExecutionEvent

from zeroth.core.agent_runtime.provider import ProviderAdapter, ProviderRequest, ProviderResponse
from zeroth.core.econ.client import RegulusClient
from zeroth.core.econ.cost import CostEstimator


class InstrumentedProviderAdapter:
    """Wraps any ProviderAdapter to emit Regulus cost events per D-04.

    After each ainvoke() call, estimates the cost, builds an ExecutionEvent,
    fires it to Regulus via the client, and enriches the response with
    cost_usd and cost_event_id so downstream code (audit records, etc.)
    can carry cost attribution.
    """

    def __init__(
        self,
        inner: ProviderAdapter,
        regulus_client: RegulusClient,
        cost_estimator: CostEstimator,
        *,
        node_id: str,
        run_id: str,
        tenant_id: str,
        deployment_ref: str,
    ) -> None:
        self._inner = inner
        self._regulus_client = regulus_client
        self._cost_estimator = cost_estimator
        self._node_id = node_id
        self._run_id = run_id
        self._tenant_id = tenant_id
        self._deployment_ref = deployment_ref

    async def ainvoke(self, request: ProviderRequest) -> ProviderResponse:
        """Call the inner adapter, estimate cost, emit event, return enriched response."""
        start = perf_counter()
        response = await self._inner.ainvoke(request)
        elapsed_ms = int((perf_counter() - start) * 1000)

        # Extract token counts from response (may be None)
        input_tokens = 0
        output_tokens = 0
        total_tokens = 0
        if response.token_usage is not None:
            input_tokens = response.token_usage.input_tokens
            output_tokens = response.token_usage.output_tokens
            total_tokens = response.token_usage.total_tokens

        # Estimate cost -- defaults to 0 on failure
        try:
            estimated_cost = self._cost_estimator.estimate(
                request.model_name,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        except Exception:
            estimated_cost = Decimal("0")

        # Build and emit the Regulus ExecutionEvent
        event = ExecutionEvent(
            capability_id=self._node_id,
            implementation_id=request.model_name,
            model_version=request.model_name,
            token_cost_usd=estimated_cost,
            latency_ms=elapsed_ms,
            compute_time_ms=elapsed_ms,
            metadata={
                "run_id": self._run_id,
                "tenant_id": self._tenant_id,
                "deployment_ref": self._deployment_ref,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
            },
        )
        self._regulus_client.track_execution(event)

        # Return enriched response with cost attribution
        return response.model_copy(
            update={
                "cost_usd": float(estimated_cost),
                "cost_event_id": event.execution_id,
            }
        )
