"""Strict RawRecordingV1 and approved TapeV1 schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from zeroth.check.tape.normalization import (
    action_identity_v1,
    canonical_bytes,
    sha256_digest,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class AdapterIdentityV1(_StrictModel):
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)


class ModelCallObservationV1(_StrictModel):
    occurrence_id: str = Field(min_length=1)
    provider: str | None = None
    model: str | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    input_details: dict[str, int] | None = None
    output_details: dict[str, int] | None = None
    request_fingerprint: str = Field(pattern=r"^sha256:[0-9A-Za-z_-]+$")
    response_fingerprint: str = Field(pattern=r"^sha256:[0-9A-Za-z_-]+$")

    @field_validator("input_details", "output_details")
    @classmethod
    def _nonnegative_details(cls, value: dict[str, int] | None) -> dict[str, int] | None:
        if value is None:
            return value
        if any(count < 0 for count in value.values()):
            raise ValueError("usage detail counts must be nonnegative")
        return value


class ToolOccurrenceV1(_StrictModel):
    occurrence_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    input_schema_digest: str = Field(min_length=1)
    tool_call_id: str = Field(min_length=1)
    arguments: dict[str, JsonValue]
    argument_fingerprint: str = Field(min_length=1)
    side_effect: Literal["read_only", "side_effecting"]
    result_available: bool
    result: JsonValue | None = None
    error_type: str | None = None
    action_identity: str = Field(pattern=r"^actv1_[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_result_shape(self) -> Self:
        if self.result_available and self.error_type is not None:
            raise ValueError("available results cannot also carry an error")
        if not self.result_available and self.result is not None:
            raise ValueError("unavailable results cannot carry a result")
        expected_arguments = sha256_digest(self.arguments)
        if self.argument_fingerprint != expected_arguments:
            raise ValueError("argument_fingerprint does not match arguments")
        return self


class SafetyTrajectoryEventV1(_StrictModel):
    event_type: str = Field(min_length=1)
    occurrence_id: str = Field(min_length=1)
    fingerprint: str = Field(min_length=1)
    state: str | None = None


class _TrajectoryFields(_StrictModel):
    normalization_version: Literal["normalization.v1"]
    action_identity_version: Literal["action_identity.v1"]
    case_id: str = Field(min_length=1)
    scenario_run_id: str = Field(min_length=1)
    adapter: AdapterIdentityV1
    target_entrypoint_digest: str = Field(min_length=1)
    case_input: JsonValue
    invocation_config: dict[str, JsonValue]
    model_calls: list[ModelCallObservationV1]
    tool_occurrences: list[ToolOccurrenceV1]
    safety_trajectory: list[SafetyTrajectoryEventV1]
    trajectory_digest: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_trajectory_and_identities(self) -> Self:
        trajectory = [event.model_dump(mode="json") for event in self.safety_trajectory]
        if self.trajectory_digest != sha256_digest(trajectory):
            raise ValueError("trajectory_digest does not match safety_trajectory")
        for occurrence in self.tool_occurrences:
            expected = action_identity_v1(
                case_id=self.case_id,
                scenario_run_id=self.scenario_run_id,
                tool_name=occurrence.name,
                input_schema_digest=occurrence.input_schema_digest,
                tool_call_id=occurrence.tool_call_id,
                argument_fingerprint=occurrence.argument_fingerprint,
            )
            if occurrence.action_identity != expected:
                raise ValueError(
                    f"action_identity does not match occurrence {occurrence.occurrence_id}"
                )
        return self


class RawRecordingV1(_TrajectoryFields):
    schema_version: Literal["raw_recording.v1"]
    source_digest: str

    @model_validator(mode="after")
    def _verify_source_digest(self) -> Self:
        payload = self.model_dump(mode="json", exclude={"source_digest"})
        if self.source_digest != sha256_digest(payload):
            raise ValueError("source_digest does not match raw recording")
        return self

    @classmethod
    def seal(cls, **payload: Any) -> Self:
        data = {
            "schema_version": "raw_recording.v1",
            **{key: _dump_nested_models(value) for key, value in payload.items()},
        }
        data["source_digest"] = sha256_digest(data)
        return cls.model_validate(data)

    def canonical_bytes(self) -> bytes:
        return canonical_bytes(self.model_dump(mode="json"))


def _dump_nested_models(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if type(value) is list:
        return [_dump_nested_models(item) for item in value]
    if type(value) is dict:
        return {key: _dump_nested_models(item) for key, item in value.items()}
    return value


class TapeV1(_TrajectoryFields):
    schema_version: Literal["tape.v1"]
    raw_source_digest: str = Field(min_length=1)
    scrubber_version: str = Field(min_length=1)
    secret_rules_version: str = Field(min_length=1)
    reviewer_id: str = Field(min_length=1)
    approved_at: str = Field(min_length=1)
    identity_changed_by_scrubbing: bool
    curated_content_digest: str

    @field_validator("approved_at")
    @classmethod
    def _valid_approval_time(cls, value: str) -> str:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value

    @model_validator(mode="after")
    def _verify_curated_digest(self) -> Self:
        payload = self.model_dump(mode="json", exclude={"curated_content_digest"})
        if self.curated_content_digest != sha256_digest(payload):
            raise ValueError("curated_content_digest does not match tape")
        return self

    @classmethod
    def seal_from_raw(
        cls,
        raw: RawRecordingV1,
        *,
        scrubber_version: str,
        secret_rules_version: str,
        reviewer_id: str,
        approved_at: str,
        identity_changed_by_scrubbing: bool,
    ) -> Self:
        common = raw.model_dump(mode="json", exclude={"schema_version", "source_digest"})
        data = {
            "schema_version": "tape.v1",
            **common,
            "raw_source_digest": raw.source_digest,
            "scrubber_version": scrubber_version,
            "secret_rules_version": secret_rules_version,
            "reviewer_id": reviewer_id,
            "approved_at": approved_at,
            "identity_changed_by_scrubbing": identity_changed_by_scrubbing,
        }
        data["curated_content_digest"] = sha256_digest(data)
        return cls.model_validate(data)

    def canonical_bytes(self) -> bytes:
        return canonical_bytes(self.model_dump(mode="json"))
