"""Deployment-side ceilings a repository manifest is evaluated against.

A manifest *requests* resources; this policy says what a deployment will
*grant*. It is pure data with conservative defaults -- no settings import, so
the contract layer stays free of configuration machinery. The service layer
constructs one from its own settings and hands it to
:func:`zeroth.contracts.repo_manifest.evaluate_policy`.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["RepoUnitPolicy"]


@dataclass(frozen=True)
class RepoUnitPolicy:
    """Ceilings for one repository-declared execution unit.

    Attributes:
        max_cpu_cores: Most CPU cores a script may request.
        max_memory_mb: Most memory, in MiB, a script may request.
        max_timeout_seconds: Longest wall-clock timeout a script may request.
        max_processes: Largest process count a script may request.
        allow_network: Whether ``network.access: full`` is grantable at all.
            Off by default: an author-supplied script gets no network unless
            the deployment explicitly opts in.
    """

    max_cpu_cores: float = 1.0
    max_memory_mb: int = 2048
    max_timeout_seconds: int = 600
    max_processes: int = 64
    allow_network: bool = False
