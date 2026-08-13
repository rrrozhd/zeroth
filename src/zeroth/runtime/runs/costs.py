"""Cost provenance rollups for run composition and budget checks."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any

from zeroth.platform.measurement import MeasurementState


@dataclass(frozen=True, slots=True)
class CostRollup:
    """Known recorded/estimated spend plus whether the rollup is complete."""

    cost_usd: float | None
    estimated_cost_usd: float | None
    cost_measurement: MeasurementState

    @property
    def total_usd(self) -> float | None:
        """Return decision spend only when every input was measured or estimated."""
        if self.cost_measurement is MeasurementState.UNMEASURED:
            return None
        return (self.cost_usd or 0.0) + (self.estimated_cost_usd or 0.0)


def _state(value: Any, cost: Any, estimated: Any) -> MeasurementState:
    if value is not None:
        with contextlib.suppress(ValueError, TypeError):
            return MeasurementState(value)
    return (
        MeasurementState.MEASURED
        if cost is not None
        else MeasurementState.ESTIMATED
        if estimated is not None
        else MeasurementState.UNMEASURED
    )


def rollup_cost_history(history: list[Any]) -> CostRollup:
    """Aggregate history without mixing estimates into recorded dollars."""
    recorded = 0.0
    estimated_total = 0.0
    saw_recorded = False
    saw_estimated = False
    incomplete = False
    for entry in history or []:
        if isinstance(entry, dict):
            cost = entry.get("cost_usd")
            estimated = entry.get("estimated_cost_usd")
            measurement = entry.get("cost_measurement")
        else:
            cost = getattr(entry, "cost_usd", None)
            estimated = getattr(entry, "estimated_cost_usd", None)
            measurement = getattr(entry, "cost_measurement", None)
        state = _state(measurement, cost, estimated)
        incomplete |= state is MeasurementState.UNMEASURED
        if cost is not None:
            try:
                recorded += float(cost)
                saw_recorded = True
            except (TypeError, ValueError):
                incomplete = True
        if estimated is not None:
            try:
                estimated_total += float(estimated)
                saw_estimated = True
            except (TypeError, ValueError):
                incomplete = True
        if state is MeasurementState.MEASURED and cost is None:
            incomplete = True
        if state is MeasurementState.ESTIMATED and estimated is None:
            incomplete = True
    state = (
        MeasurementState.UNMEASURED
        if incomplete or not (saw_recorded or saw_estimated)
        else MeasurementState.ESTIMATED
        if saw_estimated
        else MeasurementState.MEASURED
    )
    return CostRollup(
        cost_usd=recorded if saw_recorded else None,
        estimated_cost_usd=estimated_total if saw_estimated else None,
        cost_measurement=state,
    )


def rollup_run_cost(run: Any) -> CostRollup:
    """Read a composed run rollup, falling back to its typed history."""
    metadata = run.metadata
    keys = {"total_cost_usd", "total_estimated_cost_usd", "cost_measurement"}
    if keys.intersection(metadata):
        cost = metadata.get("total_cost_usd")
        estimated = metadata.get("total_estimated_cost_usd")
        return CostRollup(
            cost_usd=float(cost) if cost is not None else None,
            estimated_cost_usd=float(estimated) if estimated is not None else None,
            cost_measurement=_state(metadata.get("cost_measurement"), cost, estimated),
        )
    return rollup_cost_history(run.execution_history)
