"""Isolation policy for registered MCP server processes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import uuid4

from zeroth.runtime.agents._mcp_docker_transport import DockerStdioWorkload as _DockerStdioWorkload
from zeroth.runtime.agents.mcp import MCPServerConfig, RegisteredMCPServerConfig

_DOCKER_CONTROL_ENVIRONMENT = {
    "BUILDKIT_PROGRESS",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
}


class MCPEnvironmentDeniedError(PermissionError):
    """A registration requested an environment key outside the operator allowlist."""

    def __init__(self, keys: set[str]) -> None:
        rendered = ", ".join(sorted(keys))
        super().__init__(f"MCP environment keys are not operator-approved: {rendered}")
        self.keys = frozenset(keys)


@dataclass(frozen=True, slots=True)
class MCPDockerIsolationConfig:
    """Operator-owned Docker isolation profile for MCP stdio servers."""

    image: str
    docker_binary: str = "docker"
    network: str = "none"
    allowed_environment_keys: tuple[str, ...] = ()
    run_as_user: str = "65534:65534"
    cpus: str = "1.0"
    memory: str = "256m"
    pids_limit: int = 64

    def __post_init__(self) -> None:
        if not re.fullmatch(r"(?:[^\s@]+@sha256:|sha256:)[0-9a-f]{64}", self.image):
            raise ValueError("MCP isolation image must be digest-pinned")
        if not self.docker_binary or "\x00" in self.docker_binary:
            raise ValueError("MCP docker binary must be a non-empty argv value")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", self.network):
            raise ValueError("MCP Docker network must be a Docker network name")
        if not re.fullmatch(r"[0-9]+(?::[0-9]+)?", self.run_as_user):
            raise ValueError("MCP run_as_user must be a numeric uid[:gid]")
        if self.pids_limit <= 0:
            raise ValueError("MCP pids_limit must be positive")
        invalid_keys = {
            key
            for key in self.allowed_environment_keys
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
        }
        if invalid_keys:
            raise ValueError("MCP allowed environment keys must be environment names")
        docker_control = {
            key
            for key in self.allowed_environment_keys
            if key.startswith("DOCKER_") or key in _DOCKER_CONTROL_ENVIRONMENT
        }
        if docker_control:
            raise ValueError("Docker control environment cannot be passed by MCP registrations")


class MCPProcessIsolator:
    """Translate a registered command into an isolated stdio transport command."""

    def __init__(self, config: MCPDockerIsolationConfig) -> None:
        self.config = config

    def isolate(self, config: RegisteredMCPServerConfig) -> MCPServerConfig:
        environment = dict(config.env or {})
        denied = set(environment) - set(self.config.allowed_environment_keys)
        if denied:
            raise MCPEnvironmentDeniedError(denied)
        args = [
            "run",
            "--rm",
            "-i",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--user",
            self.config.run_as_user,
            "--cpus",
            self.config.cpus,
            "--memory",
            self.config.memory,
            "--pids-limit",
            str(self.config.pids_limit),
            "--network",
            self.config.network,
        ]
        for key in sorted(environment):
            args.extend(["--env", key])
        args.extend([self.config.image, config.command, *config.args])
        transport = MCPServerConfig(
            name=config.name,
            command=self.config.docker_binary,
            args=args,
            env=environment or None,
        )
        # This metadata is private runtime state, never an author-controlled
        # model field. Serialized server configs cannot claim cleanup ownership.
        name = f"zeroth-mcp-workload-{uuid4().hex}"
        transport._docker_workload = _DockerStdioWorkload(
            docker_binary=self.config.docker_binary,
            container_name=name,
            create_args=("create", "--name", name, *args[2:]),
        )
        return transport


def mcp_process_isolator_from_settings(settings: object) -> MCPProcessIsolator | None:
    """Build the runtime adapter from the platform sandbox settings."""
    image = getattr(settings, "mcp_isolation_image", None)
    if image is None:
        return None
    return MCPProcessIsolator(
        MCPDockerIsolationConfig(
            image=image,
            docker_binary=getattr(settings, "docker_binary", "docker"),
            network=getattr(settings, "mcp_isolation_network", "none"),
            allowed_environment_keys=tuple(
                getattr(settings, "mcp_isolation_allowed_environment_keys", ())
            ),
            run_as_user=getattr(settings, "mcp_isolation_run_as_user", "65534:65534"),
            cpus=getattr(settings, "mcp_isolation_cpus", "1.0"),
            memory=getattr(settings, "mcp_isolation_memory", "256m"),
            pids_limit=getattr(settings, "mcp_isolation_pids_limit", 64),
        )
    )


__all__ = [
    "MCPDockerIsolationConfig",
    "MCPEnvironmentDeniedError",
    "MCPProcessIsolator",
    "mcp_process_isolator_from_settings",
]
