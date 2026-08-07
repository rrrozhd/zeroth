"""Bounded response observation and content-free gateway audit projection.

This module holds the two halves of the gateway's evidence path:
:class:`TeeObserver`, which hashes a response body as it streams past without
retaining it, and :class:`AuditGatewayEventSink`, which projects one terminal
:class:`~zeroth.contracts.langgraph_gateway.models.GatewayEvent` into one
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

**Loss is counted here, never raised at the caller.** :meth:`AuditGatewayEventSink.emit`
returns normally whatever happens: a refused hand-off, a record the projection
could not build, a record the delivery stage refuses to account for. Each is
counted through that stage's own counters, which is what puts it on
``/v1/metrics`` and in the readiness probe. Raising was worse than useless: the
proxy's generic handler logged it with a full traceback, so *saturation* --
the moment the process can least afford it -- produced one traceback per refused
event on the response-completion path, carrying whatever a foreign sink's
exception message holds. A counter is scraped; a log line is not.

*Including a hand-off that raises rather than refusing.* An injected submitter
raising ``RuntimeError`` escaped past a guard that caught only ``ValueError``,
restoring the traceback-per-event with its message. Every ordinary exception
from the hand-off, and from the accounting that answers it, is now a counted
rejection; :class:`asyncio.CancelledError` is re-raised ahead of all of it.

**What the proxy's own guards mean now.** ``GatewayProxy`` still wraps this call
in ``asyncio.timeout(event_sink_timeout_seconds)``, but that is a *cooperative*
guard and nothing more: synchronous work before the first await, a sink that
blocks the event loop, or one that swallows cancellation all escape it -- which
is why the contract is stated as a hard requirement on
:class:`AuditRecordSubmitter` rather than assumed.
``_TerminalEmissionState.attempted`` is still set before the call, and means
"handed off", not "persisted": delivery is the queue's business and has its own
counters. That ordering keeps one request mapped to exactly one ``audit_id``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable, Mapping
from typing import Protocol
from uuid import uuid4

from zeroth.contracts.langgraph_gateway.models import GatewayEvent
from zeroth.governance.audit import NodeAuditRecord
from zeroth.governance.audit.delivery import (
    AuditDeliveryQueue,
    AuditRecordWriter,
    DeliveryRejection,
)
from zeroth.governance.identity import ActorIdentity

logger = logging.getLogger(__name__)

_IDENTIFIER_KEYS = ("run_id", "thread_id", "assistant_id")
_GATEWAY_NODE_ID = "langgraph.gateway"
# The fixed code the last resort is logged under: the accounting call itself
# raised, so there is no counter left to move and nothing but a code may be said
# about it -- the submitter is injected and its message is foreign text.
_ACCOUNTING_FAILED = "audit_accounting_failed"


class AuditRecordSubmitter(Protocol):
    """The non-blocking hand-off a terminal gateway event is projected into.

    Satisfied by :class:`~zeroth.governance.audit.delivery.AuditDeliveryQueue`.

    **An implementation MUST NOT block, suspend, or perform I/O.** Both methods
    are deliberately synchronous, and every one of them is called from inside a
    streaming response's ``finally``, between the last upstream chunk and the end
    of the client's response. The proxy wraps the call in ``asyncio.timeout``,
    but that is a *cooperative* guard: it can only interrupt an implementation at
    an ``await`` it actually reaches, so synchronous work before the first await,
    anything that blocks the event loop (a synchronous socket, a file read, a
    lock), and anything that swallows :class:`asyncio.CancelledError` all defeat
    it. The production implementation does one ``put_nowait`` onto a bounded
    queue and returns; an implementation that cannot promise the same must not
    be installed here, because no in-process guard can make it safe.
    """

    def submit(self, record: NodeAuditRecord) -> bool:
        """Queue one record, returning ``False`` when the stage refuses it."""
        ...

    def reject(self, audit_id: str, reason: DeliveryRejection) -> None:
        """Count one event that never reached the queue at all."""
        ...


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

    # noqa comment: this function's complexity predates ZER-24. The relocation
    # moved the module without changing a line of the body; reducing it here
    # would mix a behaviour-bearing refactor into a pure move.
    def _consume_sse_lines(self) -> None:  # noqa: C901
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

        Never raises. Every way this can fail -- an unprojectable record, one
        the delivery stage will not account for, a full queue -- is counted
        through that stage's counters and is visible on ``/v1/metrics`` and the
        readiness probe. Handing a refusal back as an exception put a full
        traceback on the response-completion path once per refused event.

        Args:
            event: The terminal event to project. Its identity is minted here,
                once, and every delivery attempt reuses it unchanged.
        """
        audit_id = f"{_GATEWAY_NODE_ID}:{uuid4().hex}"
        try:
            record = self._project(event, audit_id=audit_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the event is foreign input and the
            # projection validates it; a malformed one must cost this one audit
            # record, counted, rather than a traceback on the streaming path.
            self._reject(audit_id, DeliveryRejection.PROJECTION_FAILED)
            return
        try:
            # A ``False`` return and a ValueError are both already counted by the
            # stage itself -- as ``queue_full``/``closed`` and ``invalid_record``.
            self._delivery.submit(record)
        except ValueError:
            return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the submitter is injected, so the
            # hand-off can raise anything at all; letting it escape reached the
            # proxy's ``logger.exception``, which is one traceback per event
            # carrying a foreign exception message on the response path.
            self._reject(audit_id, DeliveryRejection.SUBMIT_FAILED)

    def _reject(self, audit_id: str, reason: DeliveryRejection) -> None:
        """Count one event that never became a queued record, without ever raising.

        The accounting call is itself the injected submitter's, so it can fail
        the same way the hand-off can. When it does there is no counter left to
        move: all that may be emitted is a fixed code and the exception type,
        never its message and never a traceback.

        Args:
            audit_id: The identity the sink had minted, for the counter's log line.
            reason: Why the event never became a queued record.
        """
        try:
            self._delivery.reject(audit_id, reason)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - emitting a terminal event never
            # raises at its producer, and that has to hold for the accounting too.
            logger.error(
                "gateway audit accounting failed code=%s reason=%s exception_type=%s",
                _ACCOUNTING_FAILED,
                reason.value,
                type(exc).__name__,
            )

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
    "TeeObserver",
]
