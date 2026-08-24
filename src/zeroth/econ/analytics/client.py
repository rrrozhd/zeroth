"""Thin wrapper around the Regulus InstrumentationClient.

Provides a RegulusClient that delegates to the SDK's InstrumentationClient,
with a clean stop() method that flushes pending events before shutdown.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from zeroth.econ.instrumentation import ExecutionEvent
from zeroth.econ.instrumentation.client import InstrumentationClient


class RegulusClient:
    """Zeroth-side wrapper around the Regulus InstrumentationClient.

    Holds a single InstrumentationClient and exposes track_execution()
    for fire-and-forget cost event emission, plus stop() for graceful
    shutdown that flushes the transport buffer.
    """

    def __init__(
        self,
        *,
        base_url: str = "http://localhost:8000/v1",
        timeout: float = 5.0,
        enabled: bool = True,
        headers_provider: Callable[[], dict[str, str]] | None = None,
        asgi_app: Any | None = None,
    ) -> None:
        self._base_url = base_url
        self._client = InstrumentationClient(
            base_url=base_url,
            timeout=timeout,
            enabled=enabled,
            headers_provider=headers_provider,
            asgi_app=asgi_app,
        )

    @property
    def base_url(self) -> str:
        """Return the Regulus API base URL."""
        return self._base_url

    def track_execution(self, event: ExecutionEvent) -> None:
        """Fire-and-forget: enqueue an execution event for delivery to Regulus."""
        self._client.track_execution(event)

    def stop(self) -> None:
        """Flush pending events and stop the transport thread."""
        self._client.transport.flush_once()
        self._client.transport.stop()
