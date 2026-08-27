"""Strict CheckEvidence and CheckVerdict public schemas."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from zeroth.check.verdict.reasons import ReasonCode


class CheckStatus(StrEnum):
    PASS = "pass"
    CANARY = "canary"
    BLOCK = "block"
    INVALID = "invalid"


STATUS_EXIT = {
    CheckStatus.PASS: 0,
    CheckStatus.CANARY: 10,
    CheckStatus.BLOCK: 20,
    CheckStatus.INVALID: 30,
}


class CheckEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["check_evidence.v1"] = "check_evidence.v1"
    status: CheckStatus
    reason_code: ReasonCode
    scope_key: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    details: dict[str, JsonValue] = Field(default_factory=dict)
    artifact_refs: tuple[str, ...] = ()

    @field_validator("details")
    @classmethod
    def _forbid_sensitive_detail_fields(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        forbidden = {"raw", "message", "argument", "result", "secret", "credential"}
        for key in value:
            lowered = key.lower()
            if any(term in lowered for term in forbidden):
                raise ValueError("evidence details cannot contain tape or secret-bearing fields")
        return value

    @field_validator("artifact_refs")
    @classmethod
    def _forbid_raw_artifacts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(".zeroth/check/recordings" in item for item in value):
            raise ValueError("reports cannot reference raw recording artifacts")
        return value


class PrerequisiteSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    valid: bool
    cases: int = Field(ge=0)


class OrdinarySummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    runs: int = Field(ge=0)
    matches: int = Field(ge=0)
    required: int = Field(ge=0)


class FaultSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    required: int = Field(ge=0)
    executed: int = Field(ge=0)
    safety_violations: int = Field(ge=0)


class UsageSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    model_calls: int = Field(ge=0)
    complete: bool


class ReportMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    generated_by: Literal["zeroth-core check"] = "zeroth-core check"
    artifact_refs: tuple[str, ...] = ()


class CheckVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["check_verdict.v1"] = "check_verdict.v1"
    status: CheckStatus
    exit_code: Literal[0, 10, 20, 30]
    reasons: tuple[CheckEvidence, ...]
    prerequisites: PrerequisiteSummary
    ordinary: OrdinarySummary
    faults: FaultSummary
    usage: UsageSummary
    report: ReportMetadata = Field(default_factory=ReportMetadata)

    @model_validator(mode="after")
    def _status_matches_exit_and_order(self) -> Self:
        if self.exit_code != STATUS_EXIT[self.status]:
            raise ValueError("exit_code does not match verdict status")
        expected = tuple(
            sorted(self.reasons, key=lambda item: (item.reason_code.value, item.scope_key))
        )
        if self.reasons != expected:
            raise ValueError("verdict reasons must use deterministic ordering")
        return self
