"""Issue codes, reports, and the carrying error for repo-manifest validation.

Structural precedent: :mod:`zeroth.contracts.graph.validation_errors`. Codes
are stable and safe for automated tooling; messages are rendered from
codebase-owned templates and never carry document text (the discipline of
:mod:`zeroth.platform.primitives.safe_yaml`). Location is a ``path`` tuple
whose elements pass a safe-name filter before they are recorded.

The YAML-layer codes deliberately mirror
:class:`zeroth.platform.primitives.UntrustedYamlErrorCode` value-for-value, so
a parser refusal surfaces here under the same stable string it was minted
with -- one vocabulary, whichever layer refused.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

__all__ = [
    "RepoManifestIssue",
    "RepoManifestSeverity",
    "RepoManifestValidationCode",
    "RepoManifestValidationError",
    "RepoManifestValidationReport",
]


class RepoManifestSeverity(StrEnum):
    """How serious a manifest issue is. ERROR blocks staging the unit."""

    WARNING = "warning"
    ERROR = "error"


class RepoManifestValidationCode(StrEnum):
    """Stable machine-readable refusal codes. Safe for automated tooling."""

    # The repository has no manifest at all.
    CONFIG_MISSING = "config_missing"

    # Parser-layer refusals; values mirror UntrustedYamlErrorCode exactly.
    YAML_TOO_LARGE = "yaml_too_large"
    YAML_NOT_UTF8 = "yaml_not_utf8"
    YAML_ALIAS_FORBIDDEN = "yaml_alias_forbidden"
    YAML_MULTIDOC_FORBIDDEN = "yaml_multidoc_forbidden"
    YAML_TOO_COMPLEX = "yaml_too_complex"
    YAML_TOO_DEEP = "yaml_too_deep"
    YAML_DUPLICATE_KEY = "yaml_duplicate_key"
    YAML_ROOT_NOT_MAPPING = "yaml_root_not_mapping"
    YAML_PARSE_ERROR = "yaml_parse_error"

    # Schema-layer refusals.
    SCHEMA_VERSION_UNSUPPORTED = "schema_version_unsupported"
    MANIFEST_SHAPE_INVALID = "manifest_shape_invalid"
    MULTIPLE_SCRIPTS_UNSUPPORTED = "multiple_scripts_unsupported"
    UNSUPPORTED_RUNTIME = "unsupported_runtime"
    NETWORK_MODE_UNSUPPORTED = "network_mode_unsupported"

    # Policy-layer refusals.
    NETWORK_ACCESS_DENIED_BY_POLICY = "network_access_denied_by_policy"
    RESOURCE_LIMIT_ABOVE_CEILING = "resource_limit_above_ceiling"
    ENVIRONMENT_KEY_RESERVED = "environment_key_reserved"

    # Staged-checkout refusals.
    SCRIPT_PATH_ESCAPES_CHECKOUT = "script_path_escapes_checkout"
    SCRIPT_NOT_A_FILE = "script_not_a_file"
    WORKDIR_ESCAPES_CHECKOUT = "workdir_escapes_checkout"
    WORKDIR_NOT_A_DIRECTORY = "workdir_not_a_directory"
    SMOKE_FILE_MISSING = "smoke_file_missing"
    SMOKE_PATH_ESCAPES_CHECKOUT = "smoke_path_escapes_checkout"


class RepoManifestIssue(BaseModel):
    """A single manifest problem: severity, stable code, location, template message."""

    model_config = ConfigDict(frozen=True)

    severity: RepoManifestSeverity
    code: RepoManifestValidationCode
    path: tuple[str, ...] = ()
    message: str


class RepoManifestValidationReport(BaseModel):
    """Every issue one validation pass found, in the order it was found."""

    model_config = ConfigDict(frozen=True)

    issues: tuple[RepoManifestIssue, ...] = ()

    @property
    def errors(self) -> tuple[RepoManifestIssue, ...]:
        """Only the blocking issues."""
        return tuple(
            issue for issue in self.issues if issue.severity is RepoManifestSeverity.ERROR
        )

    @property
    def has_errors(self) -> bool:
        """Whether anything in the report blocks staging."""
        return any(issue.severity is RepoManifestSeverity.ERROR for issue in self.issues)

    def raise_for_errors(self) -> None:
        """Raise :class:`RepoManifestValidationError` when the report blocks."""
        if self.has_errors:
            raise RepoManifestValidationError(self)


class RepoManifestValidationError(ValueError):
    """A manifest failed validation; ``report`` carries every issue found."""

    def __init__(self, report: RepoManifestValidationReport):
        self.report = report
        errors = len(report.errors)
        super().__init__(
            f"repository manifest failed validation: {errors} error(s), "
            f"{len(report.issues) - errors} warning(s)"
        )
