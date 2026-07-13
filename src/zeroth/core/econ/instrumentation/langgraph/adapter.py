from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Union

from zeroth.core.econ.instrumentation.client import track_execution, track_outcome
from zeroth.core.econ.instrumentation.schemas import ExecutionEvent, OutcomeEvent


class LangGraphTelemetryAdapter:
    """Simple callback-style adapter for LangGraph-like run events."""

    def on_run_end(self, run_id: str, capability_id: str, implementation_id: str, payload: dict[str, Any]) -> None:
        event = ExecutionEvent(
            execution_id=run_id,
            capability_id=capability_id,
            implementation_id=implementation_id,
            model_version=str(payload.get("model_version", "unknown")),
            token_cost_usd=Decimal(str(payload.get("token_cost_usd", "0"))),
            tool_cost_usd=Decimal(str(payload.get("tool_cost_usd", "0"))),
            compute_cost_usd=Decimal(str(payload.get("compute_cost_usd", "0"))),
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
