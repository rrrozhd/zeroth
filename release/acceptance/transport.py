"""Bounded same-origin HTTP and WebSocket transport for acceptance probes."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import SecretStr
from websockets.asyncio.client import connect as websocket_connect_default

from .config import ResolvedAcceptanceConfig


class TransportError(RuntimeError):
    """A target response violated the bounded acceptance transport contract."""


@dataclass(frozen=True)
class HttpObservation:
    """Sanitized response information consumed by scenario assertions."""

    status_code: int
    body: Any
    correlation_id: str | None


def redact(value: Any, secrets: Mapping[str, SecretStr]) -> Any:
    """Recursively replace configured credential bytes in diagnostics."""
    secret_values = tuple(secret.get_secret_value() for secret in secrets.values())

    def clean(item: Any) -> Any:
        if isinstance(item, str):
            for secret in secret_values:
                item = item.replace(secret, "[REDACTED]")
            return item
        if isinstance(item, dict):
            return {str(key): clean(nested) for key, nested in item.items()}
        if isinstance(item, list):
            return [clean(nested) for nested in item]
        if isinstance(item, tuple):
            return tuple(clean(nested) for nested in item)
        return item

    return clean(value)


class AcceptanceTransport:
    """Transport that cannot send acceptance credentials to another origin."""

    def __init__(
        self,
        config: ResolvedAcceptanceConfig,
        *,
        http_transport: httpx.AsyncBaseTransport | None = None,
        websocket_connect: Callable[..., Any] = websocket_connect_default,
        max_response_bytes: int = 1024 * 1024,
    ) -> None:
        self.config = config
        self._websocket_connect = websocket_connect
        self._max_response_bytes = max_response_bytes
        self._client = httpx.AsyncClient(
            timeout=config.timeout_seconds,
            follow_redirects=False,
            transport=http_transport,
        )

    async def __aenter__(self) -> AcceptanceTransport:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self._client.aclose()

    def _url(self, path: str, *, websocket: bool = False) -> str:
        parsed = urlsplit(path)
        if not path.startswith("/") or parsed.scheme or parsed.netloc:
            raise TransportError("request target must be an origin-relative path")
        base = urlsplit(self.config.base_url)
        scheme = {"http": "ws", "https": "wss"}[base.scheme] if websocket else base.scheme
        return urlunsplit((scheme, base.netloc, parsed.path, parsed.query, ""))

    def _headers(self, role: str | None) -> list[tuple[str, str]]:
        headers = [
            ("X-Acceptance-Tenant", self.config.tenant_id),
            ("X-Acceptance-Namespace", self.config.namespace),
        ]
        if role is None:
            return headers
        try:
            credential = self.config.credentials[role].get_secret_value()
        except KeyError as error:
            raise TransportError(f"unknown acceptance role {role!r}") from error
        return [("X-API-Key", credential), *headers]

    async def request(
        self,
        role: str | None,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
    ) -> HttpObservation:
        """Issue one bounded request and return a sanitized observation."""
        request = self._client.build_request(
            method,
            self._url(path),
            headers=self._headers(role),
            json=json_body,
        )
        response = await self._client.send(request, stream=True)
        try:
            if 300 <= response.status_code < 400:
                raise TransportError("redirect response refused")
            body_parts: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > self._max_response_bytes:
                    raise TransportError("response exceeds acceptance size limit")
                body_parts.append(chunk)
            content = b"".join(body_parts)
            body: Any = json.loads(content) if content else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = content.decode("utf-8", errors="replace")
        finally:
            await response.aclose()
        return HttpObservation(
            status_code=response.status_code,
            body=redact(body, self.config.credentials),
            correlation_id=response.headers.get("X-Correlation-ID"),
        )

    async def websocket_events(
        self,
        role: str,
        path: str,
        payload: Any,
        *,
        max_events: int,
    ) -> list[Any]:
        """Send one WebSocket message and collect a bounded number of events."""
        if max_events < 1 or max_events > 10_000:
            raise TransportError("max_events must be between 1 and 10000")
        events: list[Any] = []
        try:
            async with asyncio.timeout(self.config.timeout_seconds):
                async with self._websocket_connect(
                    self._url(path, websocket=True),
                    additional_headers=self._headers(role),
                    max_size=self._max_response_bytes,
                ) as socket:
                    await socket.send(json.dumps(payload, separators=(",", ":")))
                    async for raw in socket:
                        if isinstance(raw, bytes) and len(raw) > self._max_response_bytes:
                            raise TransportError("WebSocket event exceeds acceptance size limit")
                        if isinstance(raw, str) and len(raw.encode()) > self._max_response_bytes:
                            raise TransportError("WebSocket event exceeds acceptance size limit")
                        try:
                            event = json.loads(raw)
                        except (json.JSONDecodeError, UnicodeDecodeError) as error:
                            raise TransportError("WebSocket event is not JSON") from error
                        events.append(redact(event, self.config.credentials))
                        if len(events) == max_events:
                            break
        except TimeoutError as error:
            raise TransportError("WebSocket acceptance deadline exceeded") from error
        return events
