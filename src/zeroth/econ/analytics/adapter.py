"""InstrumentedProviderAdapter -- cost-tracking decorator for any ProviderAdapter.

Wraps any ProviderAdapter to:
1. Measure wall-clock latency of the LLM call
2. Estimate USD cost via CostEstimator (litellm pricing)
3. Emit a Regulus ExecutionEvent via RegulusClient (fire-and-forget)
4. Return enriched ProviderResponse with cost_usd and cost_event_id

Exception -- cache hits: when an inner ``CachingProviderAdapter`` short-circuits
the call (``response.metadata["cache_hit"] is True``) no model was reached, so
steps 3-4 would fabricate cost and double-bill Regulus. On a hit the adapter emits
no event and attributes zero marginal cost, recording the avoided spend as
``metadata["cache_saved_usd"]`` for savings visibility.
"""

from __future__ import annotations

import contextlib
from decimal import Decimal
from time import perf_counter

from zeroth.econ.analytics.client import RegulusClient
from zeroth.econ.analytics.cost import CostEstimator
from zeroth.econ.instrumentation import ExecutionEvent
from zeroth.econ.measurement import MeasurementState
from zeroth.runtime.agents.provider import ProviderAdapter, ProviderRequest, ProviderResponse


class InstrumentedProviderAdapter:
    """Wraps any ProviderAdapter to emit Regulus cost events per D-04.

    After each ainvoke() call, estimates the cost, builds an ExecutionEvent,
    fires it to Regulus via the client, and enriches the response with
    cost_usd and cost_event_id so downstream code (audit records, etc.)
    can carry cost attribution.

    A cache hit is the one exception: when an inner ``CachingProviderAdapter``
    short-circuits the call (``response.metadata["cache_hit"] is True``), there is
    no new spend, so no event is emitted and zero marginal cost is attributed
    (see ``ainvoke``).
    """

    def __init__(
        self,
        inner: ProviderAdapter,
        regulus_client: RegulusClient | None,
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
        """Call the inner adapter, estimate cost, emit event, return enriched response.

        A cache hit (set by an inner ``CachingProviderAdapter``) is the exception: no
        model was reached, so no event is emitted and zero marginal cost is attributed
        -- see the cache-hit branch below.
        """
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

        estimated_cost: Decimal | None = None
        if response.token_usage is not None:
            with contextlib.suppress(Exception):
                estimated_cost = self._cost_estimator.estimate(
                    request.model_name,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )

        # Cache hit: an inner CachingProviderAdapter short-circuited the model call,
        # so there is no new spend. Emitting an event or stamping cost here would
        # fabricate cost and double-bill Regulus for a call that never reached a
        # model. Attribute zero marginal cost and emit no event; record what the call
        # would have cost as cache_saved_usd for savings visibility. Preserve
        # cache_hit=True so econ.waste's cache_inefficiency detector still sees it.
        if response.metadata.get("cache_hit") is True:
            return response.model_copy(
                update={
                    "cost_usd": 0.0,
                    "cost_measurement": MeasurementState.MEASURED,
                    "cost_event_id": None,
                    "metadata": {
                        **response.metadata,
                        "cache_saved_usd": (
                            float(estimated_cost) if estimated_cost is not None else None
                        ),
                    },
                }
            )

        # An inner instrumentation layer already owns this measurement/event.
        # Repricing it here would count the same provider call twice.
        if response.cost_measurement is not MeasurementState.UNMEASURED:
            return response

        # No Regulus client (cost tracking on, event stream off): still attribute the
        # local litellm cost estimate so the audit trail and local econ lenses work,
        # but emit no event and leave cost_event_id None — there is no Regulus event to
        # reference. Mirrors the already-shipped cache-hit shape (cost_usd + no event).
        if self._regulus_client is None:
            return response.model_copy(
                update={
                    "estimated_cost_usd": (
                        float(estimated_cost) if estimated_cost is not None else None
                    ),
                    "cost_measurement": (
                        MeasurementState.ESTIMATED
                        if estimated_cost is not None
                        else MeasurementState.UNMEASURED
                    ),
                    "cost_event_id": None,
                }
            )

        # Build and emit the Regulus ExecutionEvent
        event = ExecutionEvent(
            capability_id=self._node_id,
            implementation_id=request.model_name,
            model_version=request.model_name,
            tenant_id=self._tenant_id,
            token_cost_usd=estimated_cost,
            cost_measurement=(
                MeasurementState.ESTIMATED
                if estimated_cost is not None
                else MeasurementState.UNMEASURED
            ),
            usage_measurement=(
                MeasurementState.MEASURED
                if response.token_usage is not None
                else MeasurementState.UNMEASURED
            ),
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
                "estimated_cost_usd": (
                    float(estimated_cost) if estimated_cost is not None else None
                ),
                "cost_measurement": (
                    MeasurementState.ESTIMATED
                    if estimated_cost is not None
                    else MeasurementState.UNMEASURED
                ),
                "cost_event_id": event.execution_id,
            }
        )
