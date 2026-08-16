"""Strict declaration and evidence models for generated-app certification."""

from __future__ import annotations

import hashlib
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
_HTTP_HEADER = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_IMAGE_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9./:_-]*$")
_IMPORT_REFERENCE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_.]*$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def _relative_path(value: str, field: str) -> str:
    path = PurePosixPath(value)
    unsafe = "\\" in value or any(ord(character) < 32 for character in value)
    if not value or unsafe or path.is_absolute() or ".." in path.parts or value.endswith("/"):
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
    headers_from_env: dict[str, str] = Field(default_factory=dict)

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

    @field_validator("headers_from_env")
    @classmethod
    def _safe_header_environment(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: set[str] = set()
        for header, env_name in value.items():
            if _HTTP_HEADER.fullmatch(header) is None or header.lower() == "content-type":
                raise ValueError(f"unsafe smoke HTTP header name {header!r}")
            if header.lower() in normalized:
                raise ValueError(f"duplicate smoke HTTP header name {header!r}")
            if _ENV_NAME.fullmatch(env_name) is None:
                raise ValueError(f"unsafe smoke environment variable name {env_name!r}")
            normalized.add(header.lower())
        return value


class CertificationTargets(BaseModel):
    """Structured app objects inspected by certifier-owned check implementations."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    graph_builders: list[str] = Field(min_length=1)
    contracts: str
    auth_config: str
    policy_guard: str
    frontend_path: str = "frontend"

    @field_validator("graph_builders")
    @classmethod
    def _unique_graph_builders(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("graph_builders must be unique")
        return value

    @field_validator("graph_builders", mode="before")
    @classmethod
    def _graph_references(cls, value: Any) -> Any:
        if not isinstance(value, list) or any(not _valid_import_ref(item) for item in value):
            raise ValueError("graph_builders must contain safe module:attribute references")
        return value

    @field_validator("contracts", "auth_config", "policy_guard")
    @classmethod
    def _import_reference(cls, value: str) -> str:
        if not _valid_import_ref(value):
            raise ValueError("target must be a safe module:attribute reference")
        return value

    @field_validator("frontend_path")
    @classmethod
    def _frontend_path(cls, value: str) -> str:
        return _relative_path(value, "frontend_path")


def _valid_import_ref(value: Any) -> bool:
    return isinstance(value, str) and _IMPORT_REFERENCE.fullmatch(value) is not None


class AppDeclaration(BaseModel):
    """Versioned, fail-closed certification declaration owned by an app."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2]
    app_name: str = Field(min_length=1, max_length=120)
    zeroth_version: str
    lock_path: str
    dockerfile: str
    image_reference: str = Field(min_length=1, max_length=255)
    sbom_path: str
    provenance_path: str
    targets: CertificationTargets
    smoke: SmokeSpec

    @field_validator("app_name")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value

    @field_validator("image_reference")
    @classmethod
    def _safe_image_reference(cls, value: str) -> str:
        if _IMAGE_REFERENCE.fullmatch(value) is None:
            raise ValueError("image_reference must contain only Docker reference characters")
        return value

    @field_validator("zeroth_version")
    @classmethod
    def _exact_pin(cls, value: str) -> str:
        if _EXACT_VERSION.fullmatch(value) is None:
            raise ValueError("zeroth_version must be an exact numeric version without a range")
        return value

    @field_validator("lock_path", "dockerfile", "sbom_path", "provenance_path")
    @classmethod
    def _safe_path(cls, value: str, info) -> str:
        return _relative_path(value, info.field_name)


class CandidateIdentity(BaseModel):
    """Measured identity to which the report and evidence files are bound."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    app_name: str
    app_commit: str
    zeroth_version: str
    image_reference: str
    image_digest: str
    source_digest: str

    @field_validator("app_name")
    @classmethod
    def _app_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("app_name must not be blank")
        return value

    @field_validator("app_commit")
    @classmethod
    def _commit(cls, value: str) -> str:
        if _COMMIT.fullmatch(value) is None:
            raise ValueError("app_commit must be 40 lowercase hexadecimal characters")
        return value

    @field_validator("zeroth_version")
    @classmethod
    def _version(cls, value: str) -> str:
        if _EXACT_VERSION.fullmatch(value) is None:
            raise ValueError("zeroth_version must be an exact numeric version")
        return value

    @field_validator("image_reference")
    @classmethod
    def _image_reference(cls, value: str) -> str:
        if _IMAGE_REFERENCE.fullmatch(value) is None:
            raise ValueError("image_reference contains unsafe characters")
        return value

    @field_validator("image_digest")
    @classmethod
    def _image_digest(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("image_digest must be immutable sha256:<64 lowercase hex>")
        return value

    @field_validator("source_digest")
    @classmethod
    def _source_digest(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("source_digest must be immutable sha256:<64 lowercase hex>")
        return value


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

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        return _relative_path(value, "evidence path")

    @field_validator("sha256")
    @classmethod
    def _digest(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("evidence sha256 must be sha256:<64 lowercase hex>")
        return value


class EvidenceBinding(BaseModel):
    """Bind retained SBOM and provenance bytes to one candidate identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_identity_digest: str
    sbom: EvidenceFile
    provenance: EvidenceFile

    @field_validator("candidate_identity_digest")
    @classmethod
    def _identity_digest(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("candidate_identity_digest must be sha256:<64 lowercase hex>")
        return value


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
        if self.candidate is not None and self.evidence is not None:
            expected = identity_digest(self.candidate)
            if self.evidence.candidate_identity_digest != expected:
                raise ValueError("evidence identity digest does not match the candidate")
        return self

    @classmethod
    def passed(
        cls,
        candidate: CandidateIdentity,
        sbom: Path,
        provenance: Path,
        *,
        root: Path,
    ) -> CertificationReport:
        """Build a passing report from already validated evidence files."""
        records = [
            EvidenceFile(
                path=path.resolve().relative_to(root.resolve()).as_posix(),
                sha256=file_digest(path),
            )
            for path in (sbom, provenance)
        ]
        return cls(
            status="passed",
            candidate=candidate,
            checks=[
                CheckResult(name=name, status="passed", detail=f"{name} passed")
                for name in MANDATORY_CHECKS
            ],
            evidence=EvidenceBinding(
                candidate_identity_digest=identity_digest(candidate),
                sbom=records[0],
                provenance=records[1],
            ),
        )


def identity_digest(identity: CandidateIdentity) -> str:
    """Return the canonical digest representing a measured candidate."""
    payload = json.dumps(identity.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def file_digest(path: Path) -> str:
    """Hash one retained evidence file."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def _read_json(path: Path) -> Any:
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError(f"JSON file {path} exceeds the 2 MiB report/declaration limit")
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


def validate_report(path: Path, *, root: Path | None = None) -> CertificationReport:
    """Load a report and recompute every retained identity/evidence binding."""
    report = CertificationReport.model_validate(_read_json(path))
    if report.status == "passed":
        from .evidence import validate_evidence

        validate_evidence(report, root or path.parent)
    return report
