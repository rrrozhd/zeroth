"""The `.zeroth.yaml` repository-manifest contract (ZER-37, schema version 1).

A repository declares how it may be run -- one script, its runtime, resources,
network posture, and smoke checks -- and this package owns the document's
schema, limits, refusal vocabulary, and pure validation. Filesystem semantics
against a staged checkout live in
:mod:`zeroth.integrations.execution.repo_units`; parsing of the untrusted
bytes goes through :func:`zeroth.platform.primitives.load_untrusted_yaml`,
the codebase's single sanctioned YAML door.
"""

from zeroth.contracts.repo_manifest.document import (
    InputMode,
    InputSpec,
    NetworkAccess,
    NetworkSpec,
    OutputMode,
    OutputSpec,
    RepoManifestDocument,
    RepoRuntime,
    ResourceSpec,
    ScriptSpec,
    SmokeSpec,
)
from zeroth.contracts.repo_manifest.errors import (
    RepoManifestIssue,
    RepoManifestSeverity,
    RepoManifestValidationCode,
    RepoManifestValidationError,
    RepoManifestValidationReport,
)
from zeroth.contracts.repo_manifest.limits import CONFIG_FILENAME, MAX_MANIFEST_BYTES
from zeroth.contracts.repo_manifest.policy import RepoUnitPolicy
from zeroth.contracts.repo_manifest.validation import evaluate_policy, parse_manifest_document

__all__ = [
    "CONFIG_FILENAME",
    "InputMode",
    "InputSpec",
    "MAX_MANIFEST_BYTES",
    "NetworkAccess",
    "NetworkSpec",
    "OutputMode",
    "OutputSpec",
    "RepoManifestDocument",
    "RepoManifestIssue",
    "RepoManifestSeverity",
    "RepoManifestValidationCode",
    "RepoManifestValidationError",
    "RepoManifestValidationReport",
    "RepoRuntime",
    "RepoUnitPolicy",
    "ResourceSpec",
    "ScriptSpec",
    "SmokeSpec",
    "evaluate_policy",
    "parse_manifest_document",
]
