"""Bounded response observation and content-free gateway audit projection.

This module holds the two halves of the gateway's evidence path:
:class:`TeeObserver`, which hashes a response body as it streams past without
retaining it, and :class:`AuditGatewayEventSink`, which projects one terminal
:class:`~zeroth.core.langgraph_gateway.models.GatewayEvent` into one
:class:`~zeroth.governance.audit.models.NodeAuditRecord` and hands it off.

**The invariant: emitting a terminal event never awaits the durable audit write,
and one gateway request becomes at most one audit record.**
:meth:`AuditGatewayEventSink.emit` is a coroutine only because the sink protocol
is one; the work it performs is O(1) and never suspends. It builds the record
and calls :meth:`~zeroth.governance.audit.delivery.AuditDeliveryQueue.submit` --
a plain synchronous ``put_nowait`` onto a finite queue. The database write, its
retries and its backoff all run on the delivery worker, off the response path,
so a slow or wedged audit repository can no longer stall, truncate or reorder a
streamed body. Before this hand-off existed, ``emit`` awaited
``AuditRepository.write`` directly from the streaming generator's ``finally``,
which put a locked database transaction between the last upstream chunk and the
end of the client's response.

**Identity is minted here, once, immediately before the hand-off.** The
``audit_id`` is fixed on the record that goes onto the queue, and nothing
downstream mints another: every delivery attempt re-writes the same identity, so
a retry after a partially-succeeded write is absorbed by the append-only
duplicate check instead of persisting one logical event twice. Deriving the
identity from ``correlation_id`` was rejected -- the gateway accepts a
client-supplied ``X-Correlation-ID``, so two unrelated requests can share one,
and a derived id would collapse them into a single audit record. A fresh
``uuid4`` per *event* costs nothing and cannot collide; what matters is that it
is minted per event rather than per attempt.

**Tenant rides on the record, explicitly.** The delivery worker has no ambient
request context to read a tenant from, and ``NodeAuditRecord.tenant_id`` defaults
to ``"default"`` -- which is also the reserved tenant owning the fallback
retention policy, so an omitted tenant would both misattribute the record and
give it the wrong TTL. The sink therefore always threads
``correlation.tenant_id`` onto the record, and the delivery stage refuses a blank
one, which turns "nobody supplied a tenant" into a counted, logged failure
instead of a silent write against the default tenant.

**What the proxy's own guards mean now.** ``GatewayProxy`` still wraps this call
in ``asyncio.timeout(event_sink_timeout_seconds)``; that bound is no longer the
backpressure mechanism -- the finite queue is -- but it stays as the boundary
guard for an *injected* sink that does not honour the non-blocking contract
above. ``_TerminalEmissionState.attempted`` is still set before the call, and now
means "this request's terminal event has been handed off", not "it was
persisted": delivery is the queue's business and has its own counters. That
ordering is what keeps one request mapped to exactly one ``audit_id``; a refused
hand-off raises :class:`GatewayAuditRefusedError` so the proxy counts it in
``sink_failure_count`` rather than silently re-emitting a second record.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from typing import Protocol
from uuid import uuid4

from zeroth.core.audit import NodeAuditRecord
from zeroth.core.identity import ActorIdentity
from zeroth.core.langgraph_gateway.models import GatewayEvent
from zeroth.governance.audit.delivery import AuditDeliveryQueue, AuditRecordWriter

_IDENTIFIER_KEYS = ("run_id", "thread_id", "assistant_id")
_GATEWAY_NODE_ID = "langgraph.gateway"


class AuditRecordSubmitter(Protocol):
    """The non-blocking hand-off a terminal gateway event is projected into.

    Satisfied by :class:`~zeroth.governance.audit.delivery.AuditDeliveryQueue`.
    Deliberately synchronous: a coroutine could be made to await a full queue,
    and this call sits inside a streaming response's ``finally``.
    """

    def submit(self, record: NodeAuditRecord) -> bool:
        """Queue one record, returning ``False`` when the stage refuses it."""
        ...


class GatewayAuditRefusedError(RuntimeError):
    """The delivery stage refused a terminal event, so it will not be persisted.

    Raised instead of returning quietly because the sink protocol has no return
    channel, and a refusal is the only signal that this event is lost.
    """


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
        self._observed_for_extraction = 0
        self._sse_data_lines: list[bytes] = []
        self._sse_skip_lf = False
        self._identifiers: dict[str, str] = {}
        self._hash = hashlib.sha256()
        self._size = 0
        self._disabled = False
        self._finished = False

    def __repr__(self) -> str:
        """Render the observer's mode and totals, never any observed bytes."""
        return (
            f"TeeObserver(mode={self._mode!r}, output_size_bytes={self._size}, "
            f"extraction_disabled={self._disabled})"
        )

    @property
    def identifiers(self) -> dict[str, str]:
        """A copy of the identifiers extracted so far."""
        return dict(self._identifiers)

    @property
    def output_sha256(self) -> str:
        """The SHA-256 of every byte observed, whatever the mode."""
        return self._hash.hexdigest()

    @property
    def output_size_bytes(self) -> int:
        """How many bytes have passed through the observer."""
        return self._size

    @property
    def extraction_disabled(self) -> bool:
        """Whether identifier extraction gave up on this stream."""
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
        if self._observed_for_extraction + len(chunk) > self._max_observation_bytes:
            self._disable()
            return chunk
        self._observed_for_extraction += len(chunk)
        self._buffer.extend(chunk)
        if self._mode == "sse":
            self._consume_sse_lines()
        return chunk

    def finish(self) -> None:
        """Close observation, parsing a buffered JSON body if one is complete."""
        if self._finished:
            return
        self._finished = True
        if self._mode == "json" and not self._disabled:
            self._parse_json(bytes(self._buffer))
        self._buffer.clear()

    def _consume_sse_lines(self) -> None:
        """Split the buffer into SSE lines, tolerating a split CRLF pair."""
        while True:
            if self._sse_skip_lf:
                if not self._buffer:
                    return
                if self._buffer[0] == 0x0A:
                    del self._buffer[0]
                self._sse_skip_lf = False
            if not self._buffer:
                return

            delimiter = next(
                (index for index, value in enumerate(self._buffer) if value in (0x0A, 0x0D)),
                None,
            )
            if delimiter is None:
                return

            line = bytes(self._buffer[:delimiter])
            terminator = self._buffer[delimiter]
            del self._buffer[: delimiter + 1]
            if terminator == 0x0D:
                if self._buffer and self._buffer[0] == 0x0A:
                    del self._buffer[0]
                elif not self._buffer:
                    self._sse_skip_lf = True
            self._consume_sse_line(line)
            if self._disabled:
                return

    def _consume_sse_line(self, line: bytes) -> None:
        """Collect one ``data:`` line, or dispatch the event a blank line ends."""
        if not line:
            self._dispatch_sse_event()
            return
        if not line.startswith(b"data:"):
            return
        value = line[5:]
        if value.startswith(b" "):
            value = value[1:]
        self._sse_data_lines.append(value)

    def _dispatch_sse_event(self) -> None:
        """Parse the accumulated ``data:`` lines of one complete SSE frame."""
        if not self._sse_data_lines:
            return
        raw = b"\n".join(self._sse_data_lines)
        self._sse_data_lines.clear()
        if raw != b"[DONE]":
            self._parse_json(raw)

    def _parse_json(self, raw: bytes) -> None:
        """Parse one JSON document, disabling extraction on anything malformed."""
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
        """Walk a parsed document for the first value of each known identifier."""
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
        """Give up on extraction and drop everything already extracted."""
        self._disabled = True
        self._buffer.clear()
        self._sse_data_lines.clear()
        self._identifiers.clear()


class AuditGatewayEventSink:
    """Project terminal gateway events into the bounded audit delivery stage.

    Args:
        repository: The durable audit write. Used only to build the sink's
            private delivery stage, and ignored when ``delivery`` is supplied.
        actor_for: Resolves the actor identity recorded for one event.
        delivery: The bounded hand-off records are submitted to. Omitted in
            production wiring, where the sink owns a private
            :class:`~zeroth.governance.audit.delivery.AuditDeliveryQueue` over
            ``repository``: a sink that silently fell back to writing inline
            would reintroduce exactly the blocking path this stage removes.
    """

    def __init__(
        self,
        repository: AuditRecordWriter,
        *,
        actor_for: Callable[[GatewayEvent], ActorIdentity | None],
        delivery: AuditRecordSubmitter | None = None,
    ) -> None:
        self._delivery: AuditRecordSubmitter = (
            AuditDeliveryQueue(repository) if delivery is None else delivery
        )
        self._actor_for = actor_for

    @property
    def delivery(self) -> AuditRecordSubmitter:
        """The delivery stage this sink submits to -- the owner's lifecycle seam."""
        return self._delivery

    async def emit(self, event: GatewayEvent) -> None:
        """Hand one terminal gateway event off for durable audit, without awaiting it.

        Args:
            event: The terminal event to project. Its identity is minted here,
                once, and every delivery attempt reuses it unchanged.

        Raises:
            GatewayAuditRefusedError: If the delivery stage refused the record,
                which is the only signal that this event will not be persisted.
            ValueError: If the event carries no tenant, raised by the delivery
                stage rather than allowing a silent write against the reserved
                ``"default"`` tenant and its fallback retention policy.
        """
        audit_id = f"{_GATEWAY_NODE_ID}:{uuid4().hex}"
        record = self._project(event, audit_id=audit_id)
        if not self._delivery.submit(record):
            raise GatewayAuditRefusedError(f"audit delivery refused {audit_id}")

    def _project(self, event: GatewayEvent, *, audit_id: str) -> NodeAuditRecord:
        """Build the content-free audit record for one terminal gateway event.

        Takes the identity as an argument instead of minting one: a projection
        that minted its own could be called twice for one logical event and
        persist it twice.
        """
        correlation = event.correlation
        actor = self._actor_for(event)
        return NodeAuditRecord(
            audit_id=audit_id,
            run_id=correlation.run_id or f"gateway:{correlation.correlation_id}",
            thread_id=correlation.thread_id,
            node_id=_GATEWAY_NODE_ID,
            graph_version_ref="langgraph.external",
            deployment_ref=correlation.deployment_ref,
            # Explicit, never the model default: the delivery worker has no
            # ambient tenant, and "default" owns the fallback retention policy.
            tenant_id=correlation.tenant_id,
            workspace_id=getattr(actor, "workspace_id", None),
            status=event.status.value,
            actor=actor,
            execution_metadata=self._metadata(event),
            started_at=event.started_at,
            completed_at=event.completed_at,
        )

    def _metadata(self, event: GatewayEvent) -> dict[str, object]:
        """Render one event as content-free metadata, dropping absent values."""
        correlation = event.correlation
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
        return {key: value for key, value in metadata.items() if value is not None}


__all__ = [
    "AuditGatewayEventSink",
    "AuditRecordSubmitter",
    "GatewayAuditRefusedError",
    "TeeObserver",
]
