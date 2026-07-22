"""Request/response schemas for the sandbox sidecar service."""

from __future__ import annotations

from pydantic import BaseModel, Field


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


class SidecarStatusResponse(BaseModel):
    """Status of a previously submitted execution."""

    execution_id: str
    status: str
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float | None = None
    timed_out: bool = False


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
