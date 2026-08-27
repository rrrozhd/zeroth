"""Request/response schemas for the sandbox sidecar service."""

from __future__ import annotations

import inspect

from pydantic import BaseModel, Field, model_validator

#: Wall-clock ceiling applied to a sandboxed execution that names no timeout.
#:
#: Declared here beside the field it bounds, but applied by the *executor*: this
#: model's constructor signature is pinned by the frozen protected-surface
#: fixture, so narrowing ``timeout_seconds`` to a non-optional float would be a
#: public-surface change requiring a fixture regeneration nobody has specified.
#: Resolving at the point of use closes the same measured harm -- ``None`` never
#: reaches ``asyncio.wait_for`` -- without touching the contract.
#:
#: ``timeout_seconds`` used to default to ``None`` and flow straight into
#: ``asyncio.wait_for``, where ``None`` means *wait forever* -- so a request body
#: that simply omitted the field bought an unbounded container. The in-repo path
#: reached the same value: the execution manifest and policy both default their
#: timeout to ``None`` and ``_effective_timeout`` returns ``None`` when neither
#: names one. A concrete default is what makes "no timeout given" mean a bound
#: rather than no bound.
DEFAULT_EXECUTION_TIMEOUT_SECONDS = 300.0

#: The container path every staged workspace volume mounts under.
WORKSPACE_MOUNT_ROOT = "/workspace"


def validate_workspace_relative_path(value: str) -> str:
    """Purely lexical guard for paths that land inside the workspace mount.

    Accepts only a relative POSIX path with no ``..`` part, no empty or ``.``
    part, no NUL, no backslash, and no colon (a colon would split the
    ``-v source:target:ro`` docker token the path is later embedded in). The
    message is a fixed template: the rejected value is client-controlled and
    is never echoed.
    """
    generic = "workspace paths must be clean relative POSIX paths"
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or "\x00" in value
        or ":" in value
    ):
        raise ValueError(generic)
    if any(part in ("", ".", "..") for part in value.split("/")):
        raise ValueError(generic)
    return value


class SidecarExecuteRequest(BaseModel):
    """Request payload to execute a command in a sandboxed container."""

    execution_id: str
    image: str
    command: list[str]
    input_text: str | None = None
    timeout_seconds: float | None = None
    environment: dict[str, str] = Field(default_factory=dict)
    working_directory: str = "/workspace"
    cpu_cores: float | None = None
    memory_mb: int | None = None
    max_processes: int | None = None
    network_access: bool = False  # Default: no network
    # ZER-37 workspace staging. Hidden from the reported signature (see the
    # ``__signature__`` assignment below) because this model's constructor is
    # pinned by the frozen protected-surface fixture; all three remain
    # ordinary keyword arguments and are recorded in
    # tests/contracts/test_signature_exclusions.py.
    workspace_id: str | None = None
    capture_output_file: str | None = None
    read_only_paths: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_workspace_fields(self) -> SidecarExecuteRequest:
        # Purely lexical: the sidecar re-checks ids and claims server-side.
        # Requests without a workspace keep their pre-ZER-37 semantics
        # untouched, including an arbitrary working_directory.
        if self.workspace_id is None:
            if self.capture_output_file is not None or self.read_only_paths:
                raise ValueError("workspace staging fields require workspace_id")
            return self
        if self.capture_output_file is not None:
            validate_workspace_relative_path(self.capture_output_file)
        for path in self.read_only_paths:
            validate_workspace_relative_path(path)
        for index, first in enumerate(self.read_only_paths):
            for second in self.read_only_paths[index + 1 :]:
                if (
                    first == second
                    or first.startswith(second + "/")
                    or second.startswith(first + "/")
                ):
                    raise ValueError("read_only_paths entries must not overlap")
        if self.working_directory != WORKSPACE_MOUNT_ROOT:
            prefix = WORKSPACE_MOUNT_ROOT + "/"
            if not self.working_directory.startswith(prefix):
                raise ValueError(
                    "working_directory must sit under /workspace when a "
                    "workspace is staged"
                )
            validate_workspace_relative_path(self.working_directory[len(prefix) :])
        return self


class SidecarExecuteResponse(BaseModel):
    """Response after executing a command in the sidecar."""

    execution_id: str
    status: str  # "running", "completed", "failed", "cancelled"
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float | None = None
    timed_out: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    # ZER-37: captured workspace output file. Carried on the immediate execute
    # response only; the persisted execution record always stores None so the
    # payload never sits in sidecar memory (see executor payload ageing).
    # Hidden from the pinned signature; recorded in the exclusion record.
    output_file_b64: str | None = None
    output_file_truncated: bool = False


class SidecarStatusResponse(BaseModel):
    """Status of a previously submitted execution."""

    execution_id: str
    status: str
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float | None = None
    timed_out: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    # ZER-37: always None for the payload (get_status never replays it), while
    # the truncation marker survives. Hidden from the pinned signature;
    # recorded in the exclusion record.
    output_file_b64: str | None = None
    output_file_truncated: bool = False


# The three models' constructor signatures are pinned by the frozen
# protected-surface fixture, so the ZER-37 staging fields are hidden from the
# reported signature rather than recorded as a surface change — the same idiom
# ``NodeAuditRecord`` and ``ToolCallRecord`` use. All stay ordinary keyword
# arguments, and every hidden field is recorded in
# tests/contracts/test_signature_exclusions.py::HIDDEN_CONSTRUCTOR_FIELDS.
SidecarExecuteRequest.__signature__ = inspect.signature(SidecarExecuteRequest).replace(
    parameters=[
        parameter
        for name, parameter in inspect.signature(SidecarExecuteRequest).parameters.items()
        if name not in {"workspace_id", "capture_output_file", "read_only_paths"}
    ]
)
SidecarExecuteResponse.__signature__ = inspect.signature(SidecarExecuteResponse).replace(
    parameters=[
        parameter
        for name, parameter in inspect.signature(SidecarExecuteResponse).parameters.items()
        if name not in {"output_file_b64", "output_file_truncated"}
    ]
)
SidecarStatusResponse.__signature__ = inspect.signature(SidecarStatusResponse).replace(
    parameters=[
        parameter
        for name, parameter in inspect.signature(SidecarStatusResponse).parameters.items()
        if name not in {"output_file_b64", "output_file_truncated"}
    ]
)


class SidecarHealthResponse(BaseModel):
    """Health check response from the sidecar service."""

    status: str = "ok"
    docker_available: bool = True


__all__ = [
    "SidecarExecuteRequest",
    "SidecarExecuteResponse",
    "SidecarHealthResponse",
    "SidecarStatusResponse",
]
