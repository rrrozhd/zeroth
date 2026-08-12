"""Cost cascade: try a cheap model first, escalate to the incumbent on hard failure.

A ``ProviderAdapter`` decorator that ACTS on right-sizing instead of only advising. It sends
each request to a cheaper model and falls back to the node's incumbent model only when the
cheap attempt HARD-fails -- a provider error, or a blank/empty response.

It deliberately makes **no quality judgement** (no confidence score): a silently-worse but
non-blank answer passes straight through. That is exactly why the runtime only enables this
on ``criticality == "low"`` nodes -- the cascade shapes spend against hard failures, not
against subtle degradation. Catching the latter needs a per-response eval on the hot path,
which is a different (expensive) tool.

Honesty rails: every attempt's cost flows through the inner ``InstrumentedProviderAdapter``
(this decorator sits OUTSIDE it), so no dollar is lost; the served model and whether it
escalated are recorded on ``response.metadata['cascade']`` so the audit trail shows exactly
which model answered.
"""

from __future__ import annotations

from zeroth.platform.measurement import MeasurementState
from zeroth.runtime.agents.provider import ProviderAdapter, ProviderRequest, ProviderResponse


def _is_blank_response(response: ProviderResponse) -> bool:
    """A hard-failure signal -- NOT a quality judgement.

    A tool-call turn is a valid intermediate step (never blank). Otherwise blank = no content
    or whitespace-only text; any structured or non-empty content is not blank.
    """
    if response.tool_calls:
        return False
    content = response.content
    if content is None:
        return True
    return isinstance(content, str) and not content.strip()


class CascadingProviderAdapter:
    """Cheap-first cascade: try ``cheap_model``; escalate to the incumbent on hard failure."""

    def __init__(self, inner: ProviderAdapter, *, cheap_model: str) -> None:
        self._inner = inner
        self._cheap_model = cheap_model

    async def ainvoke(self, request: ProviderRequest) -> ProviderResponse:
        incumbent_model = request.model_name  # the node's model_provider = escalation target
        cheap_request = request.model_copy(update={"model_name": self._cheap_model})

        primary_cost: float | None = None
        primary_estimated_cost: float | None = None
        primary_measurement = MeasurementState.UNMEASURED
        primary_event_id: str | None = None
        primary: ProviderResponse | None = None
        failure: str | None = None
        try:
            primary = await self._inner.ainvoke(cheap_request)
            primary_cost = primary.cost_usd
            primary_estimated_cost = primary.estimated_cost_usd
            primary_measurement = primary.cost_measurement
            primary_event_id = primary.cost_event_id
            if not _is_blank_response(primary):
                return primary.model_copy(
                    update={
                        "metadata": {
                            **primary.metadata,
                            "cascade": {
                                "served_by": self._cheap_model,
                                "escalated": False,
                                "primary_failure": None,
                                "primary_cost_usd": primary_cost,
                                "primary_estimated_cost_usd": primary_estimated_cost,
                            },
                        }
                    }
                )
            failure = "blank"
        except Exception:  # noqa: BLE001 -- escalate on ANY provider error (see module docstring)
            failure = "error"  # a raised call has no measurable completion; primary_cost stays 0

        # Escalate to the incumbent (request.model_name unchanged). If this ALSO raises, the
        # exception propagates -- both models failed; never fabricate a response.
        try:
            incumbent = await self._inner.ainvoke(request)
        except Exception as exc:
            if primary is not None:
                from zeroth.runtime.agents.runner import AgentRunner

                AgentRunner._attach_cost_audit(exc, primary)
            raise
        incumbent_cost = incumbent.cost_usd
        incumbent_estimated_cost = incumbent.estimated_cost_usd
        states = (primary_measurement, incumbent.cost_measurement)
        cost_measurement = (
            MeasurementState.UNMEASURED
            if MeasurementState.UNMEASURED in states
            else MeasurementState.ESTIMATED
            if MeasurementState.ESTIMATED in states
            else MeasurementState.MEASURED
        )
        recorded = [cost for cost in (primary_cost, incumbent_cost) if cost is not None]
        estimates = [
            cost for cost in (primary_estimated_cost, incumbent_estimated_cost) if cost is not None
        ]
        return incumbent.model_copy(
            update={
                # Attribute EVERY dollar: the cheap attempt AND the incumbent. This sums two
                # ExecutionEvents into one cost_usd; cost_event_id references the served
                # incumbent event, and the cheap event id is preserved in metadata.
                "cost_usd": sum(recorded) if recorded else None,
                "estimated_cost_usd": sum(estimates) if estimates else None,
                "cost_measurement": cost_measurement,
                "metadata": {
                    **incumbent.metadata,
                    "cascade": {
                        "served_by": incumbent_model,
                        "escalated": True,
                        "primary_failure": failure,
                        "primary_cost_usd": primary_cost,
                        "primary_estimated_cost_usd": primary_estimated_cost,
                        "primary_cost_event_id": primary_event_id,
                        "incumbent_cost_usd": incumbent_cost,
                        "incumbent_estimated_cost_usd": incumbent_estimated_cost,
                    },
                },
            }
        )
