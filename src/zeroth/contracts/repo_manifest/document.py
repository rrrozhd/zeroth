"""Pydantic schema for the `.zeroth.yaml` v1 repository manifest.

The document is author-supplied, so the configuration is the strictest in the
codebase (the :mod:`zeroth.contracts.graph.token_snapshot` precedent): unknown
keys refused, frozen, strict types, validated defaults, no inf/nan. Enum and
sequence fields carry a per-field ``strict=False`` because YAML hands the
validator plain strings and lists -- item types and patterns are still
enforced; only the container/enum coercion is relaxed.

Path fields are bounded *syntactically* here (relative, forward slashes,
no ``..`` segment, bounded length and segment count) and *semantically* by
:func:`zeroth.integrations.execution.repo_units.validate_staged_manifest`,
which resolves them against a real checkout and refuses symlink escapes. Both
layers exist on purpose: the model refuses what can be refused without a
filesystem, so a hostile path never travels further than parsing.

``network.access`` has exactly two members. ``"restricted"`` (an allowlist
mode) is reserved for a future schema version and is deliberately *not* a
member -- the validation layer names it in its refusal so authors learn it is
reserved rather than misspelled.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from zeroth.contracts.repo_manifest.limits import (
    CAPABILITY_NAME_PATTERN,
    ENVIRONMENT_KEY_PATTERN,
    MAX_CAPABILITIES,
    MAX_ENVIRONMENT_VALUE_CHARS,
    MAX_PATH_CHARS,
    MAX_PATH_SEGMENTS,
    MAX_SMOKE_FILES,
    MAX_STDOUT_CONTAINS_CHARS,
    SCRIPT_NAME_PATTERN,
)

__all__ = [
    "InputMode",
    "InputSpec",
    "NetworkAccess",
    "NetworkSpec",
    "OutputMode",
    "OutputSpec",
    "RepoManifestDocument",
    "RepoRuntime",
    "ResourceSpec",
    "ScriptSpec",
    "SmokeSpec",
]


class RepoRuntime(StrEnum):
    """Interpreters a v1 manifest may name."""

    PYTHON3 = "python3"


class InputMode(StrEnum):
    """How a script receives its input payload."""

    JSON_STDIN = "json_stdin"
    NONE = "none"


class OutputMode(StrEnum):
    """How a script reports its result."""

    JSON_STDOUT = "json_stdout"
    EXIT_CODE_ONLY = "exit_code_only"


class NetworkAccess(StrEnum):
    """Network posture a script may request. ``restricted`` is reserved."""

    NONE = "none"
    FULL = "full"


def _checked_relative_path(value: str) -> str:
    """Refuse anything but a bounded, forward-slash, root-relative path.

    Messages stay value-free: a path is author-supplied, so the failing value
    must never ride an error message out of the validator.
    """
    if "\x00" in value:
        raise ValueError("path must not contain NUL bytes")
    if "\\" in value:
        raise ValueError("path must use forward slashes only")
    if value.startswith("/"):
        raise ValueError("path must be relative to the checkout root")
    segments = value.split("/")
    if len(segments) > MAX_PATH_SEGMENTS:
        raise ValueError("path exceeds the permitted segment count")
    if any(segment == "" for segment in segments):
        raise ValueError("path must not contain empty segments")
    if ".." in segments:
        raise ValueError("path must not contain a parent-directory segment")
    return value


def _checked_working_directory(value: str) -> str:
    """Like a relative path, except ``"."`` -- the checkout root -- is legal."""
    if value == ".":
        return value
    return _checked_relative_path(value)


RelativePosixPath = Annotated[
    str,
    Field(min_length=1, max_length=MAX_PATH_CHARS),
    AfterValidator(_checked_relative_path),
]
WorkingDirectoryPath = Annotated[
    str,
    Field(min_length=1, max_length=MAX_PATH_CHARS),
    AfterValidator(_checked_working_directory),
]
ScriptName = Annotated[str, Field(pattern=SCRIPT_NAME_PATTERN)]
EnvironmentKey = Annotated[str, Field(pattern=ENVIRONMENT_KEY_PATTERN)]
EnvironmentValue = Annotated[str, Field(max_length=MAX_ENVIRONMENT_VALUE_CHARS)]
CapabilityName = Annotated[str, Field(pattern=CAPABILITY_NAME_PATTERN)]


class _ManifestModel(BaseModel):
    """Shared strictest-in-the-codebase configuration for manifest models."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        allow_inf_nan=False,
    )


class InputSpec(_ManifestModel):
    """How the runner feeds a script."""

    mode: Annotated[InputMode, Field(strict=False)] = InputMode.JSON_STDIN


class OutputSpec(_ManifestModel):
    """How the runner reads a script's result."""

    mode: Annotated[OutputMode, Field(strict=False)] = OutputMode.JSON_STDOUT


class ResourceSpec(_ManifestModel):
    """Resources a script requests. Every field optional; ceilings are policy."""

    cpu_cores: Annotated[float, Field(gt=0)] | None = None
    memory_mb: Annotated[int, Field(gt=0)] | None = None
    timeout_seconds: Annotated[int, Field(gt=0)] | None = None
    max_processes: Annotated[int, Field(gt=0)] | None = None


class NetworkSpec(_ManifestModel):
    """Network posture a script requests. Default: none."""

    access: Annotated[NetworkAccess, Field(strict=False)] = NetworkAccess.NONE


class SmokeSpec(_ManifestModel):
    """A cheap post-stage check that the checkout is the shape the script needs."""

    files_exist: Annotated[
        tuple[RelativePosixPath, ...], Field(strict=False, max_length=MAX_SMOKE_FILES)
    ] = ()
    exit_code: Annotated[int, Field(ge=0, le=255)] = 0
    stdout_contains: Annotated[str, Field(max_length=MAX_STDOUT_CONTAINS_CHARS)] | None = None


class ScriptSpec(_ManifestModel):
    """One runnable script declared by a repository."""

    entry: RelativePosixPath
    runtime: Annotated[RepoRuntime, Field(strict=False)]
    working_directory: WorkingDirectoryPath = "."
    input: InputSpec = Field(default_factory=InputSpec)
    output: OutputSpec = Field(default_factory=OutputSpec)
    environment: dict[EnvironmentKey, EnvironmentValue] = Field(default_factory=dict)
    resources: ResourceSpec = Field(default_factory=ResourceSpec)
    network: NetworkSpec = Field(default_factory=NetworkSpec)
    capabilities: Annotated[
        tuple[CapabilityName, ...], Field(strict=False, max_length=MAX_CAPABILITIES)
    ] = ()
    smoke: SmokeSpec | None = None


class RepoManifestDocument(_ManifestModel):
    """The `.zeroth.yaml` document, schema version 1."""

    schema_version: Literal[1]
    scripts: Annotated[dict[ScriptName, ScriptSpec], Field(min_length=1)]
