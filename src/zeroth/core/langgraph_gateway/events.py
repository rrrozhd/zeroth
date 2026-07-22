"""Bounded response observation and content-free gateway audit projection."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from typing import Protocol
from uuid import uuid4

from zeroth.core.audit import NodeAuditRecord
from zeroth.core.identity import ActorIdentity
from zeroth.core.langgraph_gateway.models import GatewayEvent

_IDENTIFIER_KEYS = ("run_id", "thread_id", "assistant_id")


class AuditWriter(Protocol):
    """The narrow existing audit-repository surface used by the gateway."""

    async def write(self, record: NodeAuditRecord) -> NodeAuditRecord: ...


class TeeObserver:
    """Synchronously hash a stream and extract bounded JSON identifiers.

    Observation never changes or retains references to downstream chunks. Only
    JSON responses and complete SSE ``data:`` frames are candidates for parsing.
    """

    def __init__(self, content_type: str | None, *, max_observation_bytes: int = 65_536) -> None:
        if type(max_observation_bytes) is not int or max_observation_bytes <= 0:
            raise ValueError("max_observation_bytes must be a positive integer")
        media_type = (content_type or "").partition(";")[0].strip().lower()
        self._mode = (
            "sse"
            if media_type == "text/event-stream"
            else "json"
            if media_type == "application/json" or media_type.endswith("+json")
            else "opaque"
        )
        self._max_observation_bytes = max_observation_bytes
        self._buffer = bytearray()
        self._identifiers: dict[str, str] = {}
        self._hash = hashlib.sha256()
        self._size = 0
        self._disabled = False
        self._finished = False

    def __repr__(self) -> str:
        return (
            f"TeeObserver(mode={self._mode!r}, output_size_bytes={self._size}, "
            f"extraction_disabled={self._disabled})"
        )

    @property
    def identifiers(self) -> dict[str, str]:
        return dict(self._identifiers)

    @property
    def output_sha256(self) -> str:
        return self._hash.hexdigest()

    @property
    def output_size_bytes(self) -> int:
        return self._size

    @property
    def extraction_disabled(self) -> bool:
        return self._disabled

    def observe(self, chunk: bytes) -> bytes:
        """Observe and return the exact same immutable byte object."""
        if type(chunk) is not bytes:
            raise TypeError("observed chunks must be bytes")
        if self._finished:
            raise RuntimeError("observer is already finished")
        self._hash.update(chunk)
        self._size += len(chunk)
        if self._mode == "opaque" or self._disabled:
            return chunk
        if len(self._buffer) + len(chunk) > self._max_observation_bytes:
            self._disable()
            return chunk
        self._buffer.extend(chunk)
        if self._mode == "sse":
            self._consume_sse_frames()
        return chunk

    def finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        if self._mode == "json" and not self._disabled:
            self._parse_json(bytes(self._buffer))
        self._buffer.clear()

    def _consume_sse_frames(self) -> None:
        while True:
            delimiter = self._buffer.find(b"\n\n")
            delimiter_size = 2
            crlf_delimiter = self._buffer.find(b"\r\n\r\n")
            if crlf_delimiter != -1 and (delimiter == -1 or crlf_delimiter < delimiter):
                delimiter = crlf_delimiter
                delimiter_size = 4
            if delimiter == -1:
                return
            frame = bytes(self._buffer[:delimiter])
            del self._buffer[: delimiter + delimiter_size]
            data_lines = []
            for line in frame.replace(b"\r\n", b"\n").split(b"\n"):
                if line.startswith(b"data:"):
                    value = line[5:]
                    if value.startswith(b" "):
                        value = value[1:]
                    data_lines.append(value)
            if data_lines and b"\n".join(data_lines) != b"[DONE]":
                self._parse_json(b"\n".join(data_lines))
                if self._disabled:
                    return

    def _parse_json(self, raw: bytes) -> None:
        try:
            value = json.loads(
                raw,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
            self._disable()
            return
        try:
            self._extract(value)
        except RecursionError:
            self._disable()

    def _extract(self, value: object) -> None:
        if isinstance(value, Mapping):
            for key in _IDENTIFIER_KEYS:
                candidate = value.get(key)
                if key not in self._identifiers and isinstance(candidate, str) and candidate:
                    self._identifiers[key] = candidate[:512]
            for nested in value.values():
                self._extract(nested)
        elif isinstance(value, list):
            for nested in value:
                self._extract(nested)

    def _disable(self) -> None:
        self._disabled = True
        self._buffer.clear()
        self._identifiers.clear()


class AuditGatewayEventSink:
    """Project terminal gateway events into the existing node audit repository."""

    def __init__(
        self,
        repository: AuditWriter,
        *,
        actor_for: Callable[[GatewayEvent], ActorIdentity | None],
    ) -> None:
        self._repository = repository
        self._actor_for = actor_for

    async def emit(self, event: GatewayEvent) -> None:
        correlation = event.correlation
        actor = self._actor_for(event)
        metadata = {
            "correlation_id": correlation.correlation_id,
            "assistant_id": correlation.assistant_id,
            "operation": event.operation,
            "disposition": event.disposition.value,
            "governance_level": event.governance_level.value,
            "policy_version": event.policy_version,
            "budget_spend_usd": event.budget_spend_usd,
            "budget_cap_usd": event.budget_cap_usd,
            "budget_check_degraded": event.budget_check_degraded,
            "compatibility_fingerprint": event.compatibility_fingerprint,
            "upstream_status_code": event.upstream_status_code,
            "input_sha256": event.input_sha256,
            "input_size_bytes": event.input_size_bytes,
            "output_sha256": event.output_sha256,
            "output_size_bytes": event.output_size_bytes,
            "duration_ms": max(0.0, (event.completed_at - event.started_at).total_seconds() * 1000),
        }
        await self._repository.write(
            NodeAuditRecord(
                audit_id=f"langgraph.gateway:{uuid4().hex}",
                run_id=correlation.run_id or f"gateway:{correlation.correlation_id}",
                thread_id=correlation.thread_id,
                node_id="langgraph.gateway",
                graph_version_ref="langgraph.external",
                deployment_ref=correlation.deployment_ref,
                tenant_id=correlation.tenant_id,
                workspace_id=getattr(actor, "workspace_id", None),
                status=event.status.value,
                actor=actor,
                execution_metadata={
                    key: value for key, value in metadata.items() if value is not None
                },
                started_at=event.started_at,
                completed_at=event.completed_at,
            )
        )


__all__ = ["AuditGatewayEventSink", "TeeObserver"]
