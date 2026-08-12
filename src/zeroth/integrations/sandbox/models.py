"""Request/response schemas for the sandbox sidecar service."""

from __future__ import annotations

from pydantic import BaseModel, Field

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
