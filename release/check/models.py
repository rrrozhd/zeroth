"""Strict identity-bound Check release evidence schema."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

REQUIRED_GATES = (
    "uv run --extra langgraph pytest tests/check -q",
    "uv run ruff check src/zeroth/check tests/check scripts/zeroth_check_action.py",
    "uv run --extra langgraph pytest tests/integrations/langgraph -q",
    "uv run pytest tests/architecture/test_wheel_packaging.py -q",
    "uv run python scripts/check_docs_references.py",
    "uv run --extra docs mkdocs build --strict",
)

REQUIRED_SCHEMAS = {
    "tape": "tape.v1",
    "normalization": "normalization.v1",
    "action_identity": "action_identity.v1",
    "fault_spec": "fault_spec.v1",
    "check_evidence": "check_evidence.v1",
    "check_verdict": "check_verdict.v1",
}


class GateEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    command: str = Field(min_length=1)
    exit_status: Literal[0]
    completed_at: str = Field(min_length=1)


class WheelEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    filename: str = Field(pattern=r"^zeroth_core-.*\.whl$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class AdapterEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    name: Literal["langgraph"]
    version: Literal["1"]
    dependency_version: str = Field(min_length=1)


class CheckReleaseEvidenceV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal["check_release_evidence.v1"]
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    package_version: str = Field(min_length=1)
    schemas: dict[str, str]
    adapter: AdapterEvidence
    wheel: WheelEvidence
    golden_fixture_sha256: dict[str, str]
    gates: tuple[GateEvidence, ...]

    @model_validator(mode="after")
    def _exact_contract(self) -> Self:
        if self.schemas != REQUIRED_SCHEMAS:
            raise ValueError("release evidence must bind all six exact V1 schemas")
        if tuple(item.command for item in self.gates) != REQUIRED_GATES:
            raise ValueError("release evidence gates do not match the authoritative ordered set")
        if not self.golden_fixture_sha256:
            raise ValueError("release evidence needs golden fixture digests")
        if any(len(value) != 64 for value in self.golden_fixture_sha256.values()):
            raise ValueError("golden fixture digests must be SHA-256 hex")
        return self
