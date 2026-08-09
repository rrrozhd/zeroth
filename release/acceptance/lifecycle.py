"""Platform lifecycle control for the deployed candidate.

Restart and drain are operations a platform performs *on* a deployment, not routes the
deployment serves. Separating them here lets the remote leg drive whatever the hosting
platform exposes while the ephemeral leg supervises a process it owns, without either
one requiring the product to ship a self-restart endpoint.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .config import LifecycleConfig


class LifecycleError(RuntimeError):
    """A lifecycle operation did not reach its expected platform outcome."""


@runtime_checkable
class LifecycleController(Protocol):
    """Enact platform lifecycle operations against the candidate."""

    async def restart(self) -> None:
        """Replace the serving process and return once it is serving again."""
        ...

    async def shutdown(self) -> None:
        """Begin draining and return once readiness has been withdrawn."""
        ...


class HttpLifecycleController:
    """Drive platform-owned lifecycle endpoints on the deployment's own origin."""

    def __init__(self, transport: Any, config: LifecycleConfig) -> None:
        self._transport = transport
        self._config = config

    async def _post(self, path: str, expected: int) -> None:
        response = await self._transport.request("admin", "POST", path)
        if response.status_code != expected:
            raise LifecycleError(
                f"lifecycle endpoint {path} expected HTTP {expected}, got {response.status_code}"
            )

    async def restart(self) -> None:
        await self._post(self._config.restart_url, self._config.restart_status)

    async def shutdown(self) -> None:
        await self._post(self._config.shutdown_url, self._config.shutdown_status)
