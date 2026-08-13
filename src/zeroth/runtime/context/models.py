"""Pydantic models for the context window management subsystem.

Defines the core data shapes: ContextWindowSettings for per-node
configuration, CompactionResult for compaction output, and
CompactionState for tracking compaction history.
"""

from __future__ import annotations

import inspect
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

# Re-exported as zeroth.runtime.context API: the authored per-node settings
# are graph contract vocabulary; the compaction result objects stay here.
from zeroth.contracts.graph.models import ContextWindowSettings as ContextWindowSettings
from zeroth.governance.audit.models import TokenUsage
from zeroth.platform.measurement import MeasurementState


class CompactionResult(BaseModel):
    """The result of a compaction operation.

    Captures the compacted message list alongside metrics about what
    changed and which strategy was used.
    """

    model_config = ConfigDict(extra="forbid")

    messages: list[Any]
    original_count: int
    compacted_count: int
    tokens_before: int
    tokens_after: int
    strategy_name: str
    archived_messages: list[Any] | None = None
    token_usage: TokenUsage | None = None
    cost_usd: float | None = None
    estimated_cost_usd: float | None = None
    cost_measurement: MeasurementState | None = None
    usage_measurement: MeasurementState | None = None

    @model_validator(mode="after")
    def _infer_measurement(self) -> CompactionResult:
        if self.cost_measurement is None:
            self.cost_measurement = (
                MeasurementState.MEASURED
                if self.cost_usd is not None
                else MeasurementState.ESTIMATED
                if self.estimated_cost_usd is not None
                else MeasurementState.UNMEASURED
            )
        if self.usage_measurement is None:
            self.usage_measurement = (
                MeasurementState.MEASURED
                if self.token_usage is not None
                else MeasurementState.UNMEASURED
            )
        return self


CompactionResult.__signature__ = inspect.signature(CompactionResult).replace(
    parameters=[
        parameter
        for name, parameter in inspect.signature(CompactionResult).parameters.items()
        if name
        not in {
            "token_usage",
            "cost_usd",
            "estimated_cost_usd",
            "cost_measurement",
            "usage_measurement",
        }
    ]
)


class CompactionState(BaseModel):
    """Tracks the current state of context window compaction.

    Used by the tracker to report its internal counters and history.
    """

    model_config = ConfigDict(extra="forbid")

    accumulated_tokens: int = 0
    max_tokens: int = 0
    compaction_count: int = 0
    last_compaction_strategy: str | None = None
