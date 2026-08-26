"""Pure validation of manifest bytes: parse, schema, and policy evaluation.

No filesystem access happens here -- checkout-dependent checks live in
:mod:`zeroth.integrations.execution.repo_units`. The message discipline is the
one :mod:`zeroth.platform.primitives.safe_yaml` establishes: pydantic's raw
messages are dropped (they quote the failing input), every surfaced message is
a codebase-owned template interpolating only counts, ceilings, and enum member
lists, and issue-path elements pass a safe-name filter (the
``governance.audit.capture_projection`` idiom) so a hostile mapping key cannot
ride a location tuple into a log line.
"""

from __future__ import annotations

import re

from pydantic import ValidationError

from zeroth.contracts.repo_manifest.document import (
    NetworkAccess,
    RepoManifestDocument,
    RepoRuntime,
)
from zeroth.contracts.repo_manifest.errors import (
    RepoManifestIssue,
    RepoManifestSeverity,
    RepoManifestValidationCode,
    RepoManifestValidationReport,
)
from zeroth.contracts.repo_manifest.limits import (
    ENVIRONMENT_KEY_PATTERN,
    MAX_MANIFEST_BYTES,
    SCRIPT_NAME_PATTERN,
)
from zeroth.contracts.repo_manifest.policy import RepoUnitPolicy
from zeroth.platform.primitives import UntrustedYamlError, load_untrusted_yaml

__all__ = [
    "evaluate_policy",
    "parse_manifest_document",
]

_CONTEXT = "repository manifest"
_REDACTED = "***REDACTED***"

# Every field name the schema declares, plus pydantic's dict-key marker.
# Anything else appearing in an error location was written by the author.
_KNOWN_LOCATION_NAMES = frozenset(
    {
        "schema_version",
        "scripts",
        "entry",
        "runtime",
        "working_directory",
        "input",
        "output",
        "environment",
        "resources",
        "network",
        "capabilities",
        "smoke",
        "mode",
        "access",
        "cpu_cores",
        "memory_mb",
        "timeout_seconds",
        "max_processes",
        "files_exist",
        "exit_code",
        "stdout_contains",
        "[key]",
    }
)
_SCRIPT_NAME_RE = re.compile(SCRIPT_NAME_PATTERN)
_ENV_KEY_RE = re.compile(ENVIRONMENT_KEY_PATTERN)

_SUPPORTED_RUNTIMES = ", ".join(member.value for member in RepoRuntime)
_SUPPORTED_NETWORK_MODES = ", ".join(member.value for member in NetworkAccess)

# The mode a future schema version will define; named in refusals so an author
# learns it is reserved, not misspelled.
_RESERVED_NETWORK_MODE = "restricted"

# Environment names the runner owns. Exact match or prefix match.
_RESERVED_ENVIRONMENT_KEYS = frozenset({"PATH"})
_RESERVED_ENVIRONMENT_PREFIXES = ("ZEROTH_", "PYTHON")


def _error(
    code: RepoManifestValidationCode, path: tuple[str, ...], message: str
) -> RepoManifestIssue:
    return RepoManifestIssue(
        severity=RepoManifestSeverity.ERROR, code=code, path=path, message=message
    )


def _safe_location_element(element: object) -> str:
    """Admit schema-owned names and safe-charset keys; redact everything else."""
    if isinstance(element, int):
        return str(element)
    if type(element) is str and (
        element in _KNOWN_LOCATION_NAMES
        or _SCRIPT_NAME_RE.match(element)
        or _ENV_KEY_RE.match(element)
    ):
        return element
    return _REDACTED


def _shape_issue(error: dict[str, object]) -> RepoManifestIssue:
    """Map one pydantic error to an issue, dropping pydantic's own message."""
    loc = tuple(error.get("loc", ()) or ())
    path = tuple(_safe_location_element(element) for element in loc)
    error_type = error.get("type")

    if error_type == "enum" and loc and loc[-1] == "runtime":
        return _error(
            RepoManifestValidationCode.UNSUPPORTED_RUNTIME,
            path,
            f"runtime is not supported in v1; supported runtimes: {_SUPPORTED_RUNTIMES}",
        )
    if error_type == "enum" and len(loc) >= 2 and loc[-2:] == ("network", "access"):
        if error.get("input") == _RESERVED_NETWORK_MODE:
            message = (
                f'network access "{_RESERVED_NETWORK_MODE}" is reserved for a future '
                f"schema version; v1 supports: {_SUPPORTED_NETWORK_MODES}"
            )
        else:
            message = (
                "network access mode is not supported in v1; "
                f"supported modes: {_SUPPORTED_NETWORK_MODES}"
            )
        return _error(RepoManifestValidationCode.NETWORK_MODE_UNSUPPORTED, path, message)
    return _error(
        RepoManifestValidationCode.MANIFEST_SHAPE_INVALID,
        path,
        "field does not satisfy the v1 manifest schema",
    )


def parse_manifest_document(
    data: bytes,
) -> tuple[RepoManifestDocument | None, RepoManifestValidationReport]:
    """Parse manifest bytes into a validated document, or report why not.

    Pure: no filesystem access. Stage a checkout and call
    :func:`zeroth.integrations.execution.repo_units.validate_staged_manifest`
    for the path-semantics half.

    Args:
        data: The raw `.zeroth.yaml` bytes, exactly as fetched.

    Returns:
        ``(document, report)``. The document is ``None`` whenever the report
        carries errors -- fail-closed: a document that parsed but was refused
        semantically (for example, two scripts) is still withheld.
    """
    issues: list[RepoManifestIssue] = []

    try:
        raw = load_untrusted_yaml(data, max_bytes=MAX_MANIFEST_BYTES, context=_CONTEXT)
    except UntrustedYamlError as exc:
        # Same stable code, and the message was rendered by safe_yaml's own
        # templates, so it is already safe to carry verbatim.
        issues.append(_error(RepoManifestValidationCode(exc.code.value), (), str(exc)))
        return None, RepoManifestValidationReport(issues=tuple(issues))

    # Peek before pydantic: bool is an int subclass and True == 1, so a strict
    # Literal[1] would admit ``schema_version: true``. The found value enters
    # the message only when it is an actual int.
    version = raw.get("schema_version")
    if type(version) is not int or version != 1:
        if type(version) is int:
            message = (
                f"schema_version {version} is not supported; "
                "this build supports schema_version 1"
            )
        else:
            message = (
                "schema_version is missing or not an integer; "
                "this build supports schema_version 1"
            )
        issues.append(
            _error(
                RepoManifestValidationCode.SCHEMA_VERSION_UNSUPPORTED,
                ("schema_version",),
                message,
            )
        )
        return None, RepoManifestValidationReport(issues=tuple(issues))

    try:
        document = RepoManifestDocument.model_validate(raw)
    except ValidationError as exc:
        issues.extend(_shape_issue(error) for error in exc.errors())
        return None, RepoManifestValidationReport(issues=tuple(issues))

    if len(document.scripts) != 1:
        issues.append(
            _error(
                RepoManifestValidationCode.MULTIPLE_SCRIPTS_UNSUPPORTED,
                ("scripts",),
                "v1 supports exactly one script; "
                f"document declares {len(document.scripts)}",
            )
        )

    report = RepoManifestValidationReport(issues=tuple(issues))
    return (document if not report.has_errors else None), report


def evaluate_policy(
    document: RepoManifestDocument, policy: RepoUnitPolicy
) -> RepoManifestValidationReport:
    """Hold a parsed document's requests to a deployment's ceilings.

    Args:
        document: A document :func:`parse_manifest_document` accepted.
        policy: The deployment's ceilings.

    Returns:
        A report whose messages name only the field and the ceiling -- both
        chosen by this codebase, never by the document.
    """
    issues: list[RepoManifestIssue] = []
    for name in sorted(document.scripts):
        script = document.scripts[name]
        prefix = ("scripts", name)

        resources = script.resources
        ceilings: tuple[tuple[str, float | int | None, float | int], ...] = (
            ("cpu_cores", resources.cpu_cores, policy.max_cpu_cores),
            ("memory_mb", resources.memory_mb, policy.max_memory_mb),
            ("timeout_seconds", resources.timeout_seconds, policy.max_timeout_seconds),
            ("max_processes", resources.max_processes, policy.max_processes),
        )
        for field_name, requested, ceiling in ceilings:
            if requested is not None and requested > ceiling:
                issues.append(
                    _error(
                        RepoManifestValidationCode.RESOURCE_LIMIT_ABOVE_CEILING,
                        (*prefix, "resources", field_name),
                        f"{field_name} exceeds the policy ceiling of {ceiling}",
                    )
                )

        if script.network.access is NetworkAccess.FULL and not policy.allow_network:
            issues.append(
                _error(
                    RepoManifestValidationCode.NETWORK_ACCESS_DENIED_BY_POLICY,
                    (*prefix, "network", "access"),
                    'network access "full" is not granted by deployment policy',
                )
            )

        for key in script.environment:
            if key in _RESERVED_ENVIRONMENT_KEYS or key.startswith(
                _RESERVED_ENVIRONMENT_PREFIXES
            ):
                issues.append(
                    _error(
                        RepoManifestValidationCode.ENVIRONMENT_KEY_RESERVED,
                        (*prefix, "environment", key),
                        "environment key is reserved by the platform "
                        '(exact "PATH", or prefix "ZEROTH_"/"PYTHON")',
                    )
                )
    return RepoManifestValidationReport(issues=tuple(issues))
