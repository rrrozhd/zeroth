"""Data models that describe executable units and how they should be run.

A "manifest" is a structured description of an executable unit -- it tells the
system what to run, how to pass data in and out, what resources it needs, and
how to identify it. Think of it like a recipe card for running code.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from governai.tools.base import ExecutionPlacement
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ExecutionMode(StrEnum):
    """How the executable unit was onboarded into the system.

    NATIVE means it is a Python function, WRAPPED_COMMAND means it wraps
    a CLI tool, and PROJECT means it is a full project with build steps.
    """

    NATIVE = "native"
    WRAPPED_COMMAND = "wrapped_command"
    PROJECT = "project"


class InputMode(StrEnum):
    """How data gets passed into an executable unit.

    For example, JSON_STDIN pipes JSON to the process's standard input,
    CLI_ARGS passes data as command-line flags, and ENV_VARS sets
    environment variables.
    """

    JSON_STDIN = "json_stdin"
    CLI_ARGS = "cli_args"
    ENV_VARS = "env_vars"
    INPUT_FILE_JSON = "input_file_json"


class OutputMode(StrEnum):
    """How data gets read back from an executable unit.

    For example, JSON_STDOUT reads JSON from standard output,
    OUTPUT_FILE_JSON reads from a file, and EXIT_CODE_ONLY just
    captures the process exit code.
    """

    JSON_STDOUT = "json_stdout"
    TAGGED_STDOUT_JSON = "tagged_stdout_json"
    OUTPUT_FILE_JSON = "output_file_json"
    TEXT_STDOUT = "text_stdout"
    EXIT_CODE_ONLY = "exit_code_only"


class EntryPointType(StrEnum):
    """What kind of thing gets executed: a Python function, a CLI command, or a project."""

    PYTHON_CALLABLE = "python_callable"
    COMMAND = "command"
    PROJECT = "project"


class RuntimeLanguage(StrEnum):
    """The runtime family used to execute the unit (Python, shell command, or project)."""

    PYTHON = "python"
    COMMAND = "command"
    PROJECT = "project"


class ArtifactSource(BaseModel):
    """Points to where the executable unit's code or binary lives.

    The `kind` says what type of artifact it is, and `ref` is a
    reference string (like a module path or command name).
    """

    model_config = ConfigDict(extra="forbid")

    kind: str
    ref: str


class PythonModuleArtifactSource(ArtifactSource):
    """Artifact source for a Python module (e.g., 'mypackage.mymodule')."""

    kind: Literal["python_module"] = "python_module"


class CommandArtifactSource(ArtifactSource):
    """Artifact source for a CLI command or executable binary."""

    kind: Literal["command"] = "command"


class ProjectArchiveArtifactSource(ArtifactSource):
    """Artifact source for a packaged project archive that needs building."""

    kind: Literal["project_archive"] = "project_archive"


ArtifactSourceType = Annotated[
    PythonModuleArtifactSource | CommandArtifactSource | ProjectArchiveArtifactSource,
    Field(discriminator="kind"),
]


class DependencySpec(BaseModel):
    """A single dependency that an executable unit needs (e.g., a Python package)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str | None = None
    source: str | None = None


class EnvironmentVariable(BaseModel):
    """An environment variable to set when running the executable unit.

    Can hold a plain value or a reference to a secret stored elsewhere.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    value: str | None = None
    secret_ref: str | None = None


class ResourceLimits(BaseModel):
    """Limits on how many resources an executable unit can use.

    These are hints for the sandbox, like max CPU cores, memory, timeout,
    number of processes, and whether network access is allowed.
    """

    model_config = ConfigDict(extra="forbid")

    cpu_cores: float | None = Field(default=None, gt=0)
    memory_mb: int | None = Field(default=None, gt=0)
    timeout_seconds: int | None = Field(default=None, gt=0)
    max_processes: int | None = Field(default=None, gt=0)
    network_access: bool | None = None


class AuditSettings(BaseModel):
    """Controls what gets recorded for auditing when an executable unit runs.

    You can toggle whether to capture stdout, stderr, inputs, and outputs.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    capture_stdout: bool = True
    capture_stderr: bool = True
    capture_inputs: bool = True
    capture_outputs: bool = True


class BuildConfig(BaseModel):
    """How to build a project-type executable unit before running it.

    Includes the build command, any extra environment variables for the build,
    and an optional hash of the dependency lock file for caching.
    """

    model_config = ConfigDict(extra="forbid")

    command: list[str] = Field(default_factory=list)
    environment: dict[str, str] = Field(default_factory=dict)
    dependency_lock_hash: str | None = None


class RunConfig(BaseModel):
    """How to actually run the executable unit.

    Includes the command to execute, what directory to run it in, and any
    extra environment variables.
    """

    model_config = ConfigDict(extra="forbid")

    command: list[str] = Field(default_factory=list)
    working_directory: str | None = None
    environment: dict[str, str] = Field(default_factory=dict)


class ExecutableUnitManifestBase(BaseModel):
    """Base class with all the fields that every manifest type shares.

    This contains everything needed to describe an executable unit: its ID,
    how it takes input, how it produces output, resource limits, dependencies,
    and more. The specific manifest types (Native, WrappedCommand, Project)
    inherit from this and add their own extra fields.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    unit_id: str
    version: int = Field(default=1, ge=1)
    onboarding_mode: ExecutionMode
    runtime: RuntimeLanguage
    artifact_source: ArtifactSourceType
    build_config: BuildConfig | None = None
    run_config: RunConfig = Field(default_factory=RunConfig)
    entrypoint_type: EntryPointType
    input_mode: InputMode
    output_mode: OutputMode
    input_contract_ref: str
    output_contract_ref: str
    environment_variables: list[EnvironmentVariable] = Field(default_factory=list)
    dependencies: list[DependencySpec] = Field(default_factory=list)
    capability_requests: list[str] = Field(default_factory=list)
    resource_limits: ResourceLimits = Field(default_factory=ResourceLimits)
    timeout_seconds: int | None = Field(default=None, gt=0)
    cache_identity_fields: dict[str, str] = Field(default_factory=dict)
    audit_settings: AuditSettings = Field(default_factory=AuditSettings)
    execution_placement: ExecutionPlacement = "local_only"
    side_effect: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _sync_timeout(self) -> ExecutableUnitManifestBase:
        """Keep the top-level timeout and resource_limits timeout in sync."""
        if self.timeout_seconds is not None and self.resource_limits.timeout_seconds is None:
            self.resource_limits.timeout_seconds = self.timeout_seconds
        elif self.timeout_seconds is None and self.resource_limits.timeout_seconds is not None:
            self.timeout_seconds = self.resource_limits.timeout_seconds
        if not self.artifact_source.ref.strip():
            raise ValueError("artifact_source.ref must not be empty")
        return self


class NativeUnitManifest(ExecutableUnitManifestBase):
    """Manifest for executable units that are plain Python functions.

    Use this when you want to run a Python callable directly, without
    spawning a subprocess. Requires a `callable_ref` pointing to the function.
    """

    onboarding_mode: Literal[ExecutionMode.NATIVE] = ExecutionMode.NATIVE
    runtime: Literal[RuntimeLanguage.PYTHON] = RuntimeLanguage.PYTHON
    entrypoint_type: Literal[EntryPointType.PYTHON_CALLABLE] = EntryPointType.PYTHON_CALLABLE
    artifact_source: PythonModuleArtifactSource
    callable_ref: str


class WrappedCommandUnitManifest(ExecutableUnitManifestBase):
    """Manifest for executable units that wrap a CLI command or script.

    Use this when you want to run an existing command-line tool as an
    executable unit. The system handles passing input and reading output.
    """

    onboarding_mode: Literal[ExecutionMode.WRAPPED_COMMAND] = ExecutionMode.WRAPPED_COMMAND
    runtime: Literal[RuntimeLanguage.COMMAND] = RuntimeLanguage.COMMAND
    entrypoint_type: Literal[EntryPointType.COMMAND] = EntryPointType.COMMAND
    artifact_source: CommandArtifactSource
    run_config: RunConfig = Field(default_factory=RunConfig)


class ProjectUnitManifest(ExecutableUnitManifestBase):
    """Manifest for executable units that are full projects needing a build step.

    Use this when the executable unit is a project archive that must be built
    (e.g., compiled or installed) before it can run.
    """

    onboarding_mode: Literal[ExecutionMode.PROJECT] = ExecutionMode.PROJECT
    runtime: Literal[RuntimeLanguage.PROJECT] = RuntimeLanguage.PROJECT
    entrypoint_type: Literal[EntryPointType.PROJECT] = EntryPointType.PROJECT
    artifact_source: ProjectArchiveArtifactSource
    build_config: BuildConfig
    project_archive_ref: str

    @model_validator(mode="after")
    def _validate_project_archive_ref(self) -> ProjectUnitManifest:
        """Make sure the project archive reference is not empty."""
        if not self.project_archive_ref.strip():
            raise ValueError("project_archive_ref must not be empty")
        return self


ExecutableUnitManifest = Annotated[
    NativeUnitManifest | WrappedCommandUnitManifest | ProjectUnitManifest,
    Field(discriminator="onboarding_mode"),
]
