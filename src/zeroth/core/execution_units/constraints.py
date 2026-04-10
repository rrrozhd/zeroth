"""Resource-constraint helpers for sandbox execution."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ResourceConstraints:
    """Runtime constraints that a hardened sandbox should enforce when possible."""

    cpu_cores: float | None = None
    memory_mb: int | None = None
    disk_mb: int | None = None
    max_processes: int | None = None
    network_access: bool | None = None

    def requires_hard_isolation(self) -> bool:
        """Return True when the request needs a hardened backend to be meaningful."""
        return any(
            value is not None
            for value in (
                self.cpu_cores,
                self.memory_mb,
                self.disk_mb,
                self.max_processes,
                self.network_access,
            )
        )


def build_docker_resource_flags(constraints: ResourceConstraints | None) -> list[str]:
    """Translate supported resource constraints into Docker CLI flags."""
    if constraints is None:
        return []
    flags: list[str] = []
    if constraints.cpu_cores is not None:
        flags.extend(["--cpus", str(constraints.cpu_cores)])
    if constraints.memory_mb is not None:
        flags.extend(["--memory", f"{constraints.memory_mb}m"])
    if constraints.max_processes is not None:
        flags.extend(["--pids-limit", str(constraints.max_processes)])
    if constraints.network_access is not None:
        flags.extend(["--network", "bridge" if constraints.network_access else "none"])
    return flags


__all__ = ["ResourceConstraints", "build_docker_resource_flags"]
