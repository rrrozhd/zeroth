"""Strict declaration and evidence models for generated-app certification."""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MANDATORY_CHECKS = (
    "graph",
    "service-config",
    "contracts",
    "dependency-lock",
    "optional-extras",
    "migrations",
    "container-startup",
    "health",
    "policies",
    "frontend-api",
    "packaged-smoke",
    "ephemeral-smoke",
    "sbom",
    "provenance",
)

_EXACT_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){1,4}$")


def _relative_path(value: str, field: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or value.endswith("/"):
        raise ValueError(f"{field} must be a safe relative file path")
    return value


def _require_json(value: Any, field: str) -> Any:
    try:
        json.dumps(value, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must contain deterministic JSON values") from error
    return value


class SmokeSpec(BaseModel):
    """One deterministic request asserted against both candidate boundaries."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    method: Literal["GET", "POST", "PUT"]
    path: str
    request_json: dict[str, Any]
    expected_status: int = Field(ge=100, le=599)
    expected_json: dict[str, Any] = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def _origin_relative_path(cls, value: str) -> str:
        parsed = urlsplit(value)
        if not value.startswith("/") or value.startswith("//"):
            raise ValueError("smoke path must be origin-relative")
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("smoke path must not contain an origin, query, or fragment")
        return value

    @field_validator("request_json", "expected_json")
    @classmethod
    def _deterministic_json(cls, value: dict[str, Any], info) -> dict[str, Any]:
        return _require_json(value, info.field_name)


class AppDeclaration(BaseModel):
    """Versioned, fail-closed certification declaration owned by an app."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    app_name: str = Field(min_length=1, max_length=120)
    zeroth_version: str
    lock_path: str
    image_reference: str = Field(min_length=1, max_length=255)
    sbom_path: str
    provenance_path: str
    checks: dict[str, list[str]]
    smoke: SmokeSpec

    @field_validator("app_name", "image_reference")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value

    @field_validator("zeroth_version")
    @classmethod
    def _exact_pin(cls, value: str) -> str:
        if _EXACT_VERSION.fullmatch(value) is None:
            raise ValueError("zeroth_version must be an exact numeric version without a range")
        return value

    @field_validator("lock_path", "sbom_path", "provenance_path")
    @classmethod
    def _safe_path(cls, value: str, info) -> str:
        return _relative_path(value, info.field_name)

    @field_validator("checks")
    @classmethod
    def _mandatory_argv(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        if set(value) != set(MANDATORY_CHECKS):
            missing = sorted(set(MANDATORY_CHECKS) - set(value))
            unknown = sorted(set(value) - set(MANDATORY_CHECKS))
            raise ValueError(f"checks must be exact; missing={missing}, unknown={unknown}")
        for name, argv in value.items():
            if not argv or any(not isinstance(arg, str) or not arg for arg in argv):
                raise ValueError(f"check {name!r} must contain a non-empty argv array")
        commands = [tuple(value[name]) for name in MANDATORY_CHECKS]
        if len(commands) != len(set(commands)):
            raise ValueError("checks must not contain duplicate commands")
        return value


class CandidateIdentity(BaseModel):
    """Measured identity to which the report and evidence files are bound."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    app_name: str
    app_commit: str
    zeroth_version: str
    image_reference: str
    image_digest: str


class CheckResult(BaseModel):
    """One mandatory check verdict with an operator-facing diagnostic."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    status: Literal["passed", "failed"]
    detail: str = Field(min_length=1)


class EvidenceFile(BaseModel):
    """Content digest of one retained evidence file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    sha256: str


class EvidenceBinding(BaseModel):
    """Bind retained SBOM and provenance bytes to one candidate identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_identity_digest: str
    sbom: EvidenceFile
    provenance: EvidenceFile


class CertificationReport(BaseModel):
    """Stable machine-readable outcome for a single app candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    status: Literal["passed", "failed"]
    candidate: CandidateIdentity | None
    checks: list[CheckResult]
    evidence: EvidenceBinding | None

    @model_validator(mode="after")
    def _coherent_verdict(self) -> CertificationReport:
        if [item.name for item in self.checks] != list(MANDATORY_CHECKS):
            raise ValueError("report must contain every mandatory check in canonical order")
        passed = all(item.status == "passed" for item in self.checks)
        if (self.status == "passed") != passed:
            raise ValueError("report status must agree with all mandatory checks")
        if passed and (self.candidate is None or self.evidence is None):
            raise ValueError("a passing report requires candidate-bound evidence")
        return self


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"JSON file {path} is unreadable: {error}") from error


def load_declaration(path: Path) -> AppDeclaration:
    """Load a declaration while refusing duplicate JSON keys."""
    return AppDeclaration.model_validate(_read_json(path))


def write_report(report: CertificationReport, path: Path) -> None:
    """Write canonical, deterministic JSON evidence."""
    rendered = json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")


def validate_report(path: Path) -> CertificationReport:
    """Load and structurally validate a retained certification report."""
    return CertificationReport.model_validate(_read_json(path))
