"""Validates that executable unit manifests are correctly structured.

Before a manifest can be used to run anything, it needs to be checked for
problems like missing IDs, invalid references, or mismatched onboarding modes.
This module does those checks and produces a detailed report of any issues.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from zeroth.execution_units.errors import ManifestValidationError
from zeroth.execution_units.models import (
    ExecutableUnitManifest,
    NativeUnitManifest,
    ProjectUnitManifest,
    WrappedCommandUnitManifest,
)


class ValidationSeverity(StrEnum):
    """How serious a validation issue is: a WARNING is informational, an ERROR blocks usage."""

    WARNING = "warning"
    ERROR = "error"


class ValidationCode(StrEnum):
    """Named codes for each type of validation problem.

    Using codes instead of free-form strings makes it easy to check for
    specific problems programmatically.
    """

    EMPTY_UNIT_ID = "empty_unit_id"
    INVALID_VERSION = "invalid_version"
    INVALID_INPUT_CONTRACT = "invalid_input_contract"
    INVALID_OUTPUT_CONTRACT = "invalid_output_contract"
    INVALID_ARTIFACT_SOURCE = "invalid_artifact_source"
    INVALID_EXECUTION_MODE = "invalid_execution_mode"
    MISSING_NATIVE_CALLABLE = "missing_native_callable"
    MISSING_COMMAND = "missing_command"
    MISSING_PROJECT_BUILD_CONFIG = "missing_project_build_config"
    MISSING_PROJECT_RUN_COMMAND = "missing_project_run_command"
    INVALID_TIMEOUT = "invalid_timeout"
    INVALID_CACHE_IDENTITY = "invalid_cache_identity"
    INVALID_DEPENDENCY = "invalid_dependency"


class ValidationIssue(BaseModel):
    """A single problem found during manifest validation.

    Contains the severity (warning or error), a code identifying the type
    of problem, a human-readable message, and the path to the problematic field.
    """

    model_config = ConfigDict(frozen=True)

    severity: ValidationSeverity
    code: ValidationCode
    message: str
    unit_id: str
    path: tuple[str, ...] = Field(default_factory=tuple)
    details: dict[str, Any] = Field(default_factory=dict)


class ManifestValidationReport(BaseModel):
    """The full results of validating a manifest.

    Contains a list of all issues found. Use `is_valid` to check if there
    are any errors, or `raise_for_errors` to throw if validation failed.
    """

    model_config = ConfigDict(frozen=True)

    unit_id: str
    issues: list[ValidationIssue] = Field(default_factory=list)

    @property
    def errors(self) -> list[ValidationIssue]:
        """Return only the error-level issues."""
        return [issue for issue in self.issues if issue.severity is ValidationSeverity.ERROR]

    @property
    def warnings(self) -> list[ValidationIssue]:
        """Return only the warning-level issues."""
        return [issue for issue in self.issues if issue.severity is ValidationSeverity.WARNING]

    @property
    def is_valid(self) -> bool:
        """Return True if there are no errors (warnings are okay)."""
        return not self.errors

    def summary(self) -> dict[str, int]:
        """Return a count of errors, warnings, and total issues."""
        return {
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "total": len(self.issues),
        }

    def raise_for_errors(self) -> None:
        """Raise ManifestValidationError if the report contains any errors."""
        if self.errors:
            raise ManifestValidationError(self)


class ExecutableUnitValidator:
    """Checks manifests for problems before they are used.

    Validates common fields (ID, version, contracts) and then runs
    mode-specific checks depending on whether the manifest is native,
    wrapped command, or project.
    """

    def validate(self, manifest: ExecutableUnitManifest) -> ManifestValidationReport:
        """Validate a manifest and return a report of all issues found."""
        issues: list[ValidationIssue] = []

        self._validate_common(manifest, issues)

        match manifest:
            case NativeUnitManifest():
                self._validate_native(manifest, issues)
            case WrappedCommandUnitManifest():
                self._validate_wrapped_command(manifest, issues)
            case ProjectUnitManifest():
                self._validate_project(manifest, issues)

        return ManifestValidationReport(unit_id=manifest.unit_id, issues=issues)

    def validate_or_raise(self, manifest: ExecutableUnitManifest) -> ManifestValidationReport:
        """Validate a manifest and raise an error if there are any problems."""
        report = self.validate(manifest)
        report.raise_for_errors()
        return report

    def _validate_common(
        self,
        manifest: ExecutableUnitManifest,
        issues: list[ValidationIssue],
    ) -> None:
        """Check fields that all manifest types must have (ID, version, contracts, etc.)."""
        if not manifest.unit_id.strip():
            self._append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.EMPTY_UNIT_ID,
                message="unit_id is required",
                unit_id=manifest.unit_id,
                path=("unit_id",),
            )
        if manifest.version < 1:
            self._append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.INVALID_VERSION,
                message="version must be positive",
                unit_id=manifest.unit_id,
                path=("version",),
                details={"version": manifest.version},
            )
        if not manifest.input_contract_ref.strip():
            self._append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.INVALID_INPUT_CONTRACT,
                message="input_contract_ref is required",
                unit_id=manifest.unit_id,
                path=("input_contract_ref",),
            )
        if not manifest.output_contract_ref.strip():
            self._append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.INVALID_OUTPUT_CONTRACT,
                message="output_contract_ref is required",
                unit_id=manifest.unit_id,
                path=("output_contract_ref",),
            )
        if manifest.timeout_seconds is not None and manifest.timeout_seconds <= 0:
            self._append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.INVALID_TIMEOUT,
                message="timeout_seconds must be positive",
                unit_id=manifest.unit_id,
                path=("timeout_seconds",),
            )
        if any(not field.strip() for field in manifest.cache_identity_fields):
            self._append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.INVALID_CACHE_IDENTITY,
                message="cache identity field names must be non-empty",
                unit_id=manifest.unit_id,
                path=("cache_identity_fields",),
            )
        if any(not value.strip() for value in manifest.cache_identity_fields.values()):
            self._append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.INVALID_CACHE_IDENTITY,
                message="cache identity values must be non-empty",
                unit_id=manifest.unit_id,
                path=("cache_identity_fields",),
            )

    def _validate_native(
        self,
        manifest: NativeUnitManifest,
        issues: list[ValidationIssue],
    ) -> None:
        """Check rules specific to native Python manifests."""
        if manifest.artifact_source.kind != "python_module":
            self._append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.INVALID_ARTIFACT_SOURCE,
                message="native units must reference a python_module artifact source",
                unit_id=manifest.unit_id,
                path=("artifact_source", "kind"),
                details={"kind": manifest.artifact_source.kind},
            )
        if not manifest.artifact_source.ref.strip():
            self._append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.INVALID_ARTIFACT_SOURCE,
                message="python_module artifact_source.ref is required",
                unit_id=manifest.unit_id,
                path=("artifact_source", "ref"),
            )
        if not manifest.callable_ref.strip():
            self._append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.MISSING_NATIVE_CALLABLE,
                message="native units require callable_ref",
                unit_id=manifest.unit_id,
                path=("callable_ref",),
            )

    def _validate_wrapped_command(
        self,
        manifest: WrappedCommandUnitManifest,
        issues: list[ValidationIssue],
    ) -> None:
        """Check rules specific to wrapped command manifests."""
        if manifest.artifact_source.kind != "command":
            self._append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.INVALID_ARTIFACT_SOURCE,
                message="wrapped command units must reference a command artifact source",
                unit_id=manifest.unit_id,
                path=("artifact_source", "kind"),
                details={"kind": manifest.artifact_source.kind},
            )
        if not manifest.artifact_source.ref.strip():
            self._append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.INVALID_ARTIFACT_SOURCE,
                message="command artifact_source.ref is required",
                unit_id=manifest.unit_id,
                path=("artifact_source", "ref"),
            )
        if not manifest.run_config.command:
            self._append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.MISSING_COMMAND,
                message="wrapped command units require a run command",
                unit_id=manifest.unit_id,
                path=("run_config", "command"),
            )

    def _validate_project(
        self,
        manifest: ProjectUnitManifest,
        issues: list[ValidationIssue],
    ) -> None:
        """Check rules specific to project archive manifests."""
        if manifest.artifact_source.kind != "project_archive":
            self._append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.INVALID_ARTIFACT_SOURCE,
                message="project units must reference a project archive artifact source",
                unit_id=manifest.unit_id,
                path=("artifact_source", "kind"),
                details={"kind": manifest.artifact_source.kind},
            )
        if not manifest.artifact_source.ref.strip():
            self._append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.INVALID_ARTIFACT_SOURCE,
                message="project archive artifact_source.ref is required",
                unit_id=manifest.unit_id,
                path=("artifact_source", "ref"),
            )
        if manifest.build_config is None:
            self._append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.MISSING_PROJECT_BUILD_CONFIG,
                message="project units require build_config",
                unit_id=manifest.unit_id,
                path=("build_config",),
            )
        if not manifest.run_config.command:
            self._append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.MISSING_PROJECT_RUN_COMMAND,
                message="project units require a run command",
                unit_id=manifest.unit_id,
                path=("run_config", "command"),
            )
        if not manifest.project_archive_ref.strip():
            self._append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.INVALID_ARTIFACT_SOURCE,
                message="project_archive_ref is required",
                unit_id=manifest.unit_id,
                path=("project_archive_ref",),
            )

    def _append_issue(
        self,
        issues: list[ValidationIssue],
        *,
        severity: ValidationSeverity,
        code: ValidationCode,
        message: str,
        unit_id: str,
        path: tuple[str, ...] = (),
        details: dict[str, Any] | None = None,
    ) -> None:
        """Helper to add a new validation issue to the issues list."""
        issues.append(
            ValidationIssue(
                severity=severity,
                code=code,
                message=message,
                unit_id=unit_id,
                path=path,
                details=dict(details or {}),
            )
        )
