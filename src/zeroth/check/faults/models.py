"""Independently versioned fault specifications and evidence facts."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class FaultName(StrEnum):
    DUPLICATE_DELIVERY = "duplicate_delivery"
    TIMEOUT_AFTER_EFFECT = "timeout_after_effect"
    CANCELLATION_AFTER_EFFECT = "cancellation_after_effect"
    RESTART_AFTER_RECEIPT = "restart_after_receipt"
    ERROR_BEFORE_EFFECT = "error_before_effect"


MANDATORY_FAULTS = (
    FaultName.DUPLICATE_DELIVERY,
    FaultName.TIMEOUT_AFTER_EFFECT,
    FaultName.CANCELLATION_AFTER_EFFECT,
    FaultName.RESTART_AFTER_RECEIPT,
)


class FaultSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["fault_spec.v1"] = "fault_spec.v1"
    case_id: str = Field(min_length=1)
    action_identity: str = Field(pattern=r"^actv1_[0-9a-f]{64}$")
    occurrence_id: str = Field(min_length=1)
    name: FaultName


class FaultEventKind(StrEnum):
    INJECTION_ARMED = "injection_armed"
    INJECTION_REACHED = "injection_reached"
    EFFECT_MARKER_WRITTEN = "effect_marker_written"
    RECEIPT_STORED = "receipt_stored"
    GRAPH_CHECKPOINT_STORED = "graph_checkpoint_stored"
    AMBIGUITY_OBSERVED = "ambiguity_observed"
    CANCELLATION_OBSERVED = "cancellation_observed"
    PROCESS_EXITED = "process_exited"
    RESUME_STARTED = "resume_started"
    RECOVERY_REACHED = "recovery_reached"
    RUN_TERMINAL = "run_terminal"


class FaultEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    event_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    action_identity: str = Field(min_length=1)
    fault_name: FaultName
    process_role: str = Field(min_length=1)
    kind: FaultEventKind


class FaultResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    spec: FaultSpec
    executed: bool
    injection_observed: bool
    recovery_observed: bool
    safety_violation: bool
    marker_count: int = Field(ge=0)
    reason_codes: tuple[str, ...] = ()


class FaultMatrixResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    results: tuple[FaultResult, ...]
    prerequisite_valid: bool
    prerequisite_reason: str | None = None
