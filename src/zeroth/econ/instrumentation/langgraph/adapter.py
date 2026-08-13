from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Union

from zeroth.econ.instrumentation.client import track_execution, track_outcome
from zeroth.econ.instrumentation.schemas import ExecutionEvent, OutcomeEvent
from zeroth.econ.measurement import MeasurementState


class LangGraphTelemetryAdapter:
    """Simple callback-style adapter for LangGraph-like run events."""

    def on_run_end(self, run_id: str, capability_id: str, implementation_id: str, payload: dict[str, Any]) -> None:
        def cost(name: str) -> Decimal | None:
            value = payload.get(name)
            return Decimal(str(value)) if value is not None else None

        event = ExecutionEvent(
            execution_id=run_id,
            capability_id=capability_id,
            implementation_id=implementation_id,
            model_version=str(payload.get("model_version", "unknown")),
            token_cost_usd=cost("token_cost_usd"),
            tool_cost_usd=cost("tool_cost_usd"),
            compute_cost_usd=cost("compute_cost_usd"),
            cost_measurement=payload.get("cost_measurement"),
            usage_measurement=payload.get(
                "usage_measurement", MeasurementState.UNMEASURED
            ),
            latency_ms=int(payload.get("latency_ms", 0)),
            compute_time_ms=int(payload.get("compute_time_ms", 0)),
            metadata=payload.get("metadata", {}),
            timestamp=datetime.now(timezone.utc),
        )
        track_execution(event)

    def emit_outcome(self, run_id: str, capability_id: str, outcome_type: str, outcome_value: Union[float, bool, str]) -> None:
        outcome = OutcomeEvent(
            execution_id=run_id,
            capability_id=capability_id,
            outcome_type=outcome_type,
            outcome_value=outcome_value,
        )
        track_outcome(outcome)
