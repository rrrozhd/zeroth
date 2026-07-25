"""The delivery stage's consume-capture-write loop.

Split from :mod:`zeroth.governance.audit.delivery` along the seam that matters
operationally: that module is what a *producer* touches -- a bounded, synchronous
hand-off and a bounded shutdown -- while everything here runs on the worker,
after the producer is long gone, and is allowed to be slow.

**The invariant: one dequeued event leaves this loop exactly once, in exactly
one counted state, and the loop survives it either way.** The three ways an
event can end -- delivered, already durable, failed -- are each counted, and
nothing else can end the loop. An exception escaping the body used to kill the
only worker, which stranded every later event in the queue *and* left the one
that caused it counted nowhere; the broad guard below is what makes "one bad
event" cost one record instead of the whole stage.

**Capture is a ladder, not a call.** The policy already fails closed
internally, so an exception reaching :meth:`DeliveryWorker._capture` means it
failed *while* failing closed. The fallback is the queue-owned
:func:`~zeroth.governance.audit.capture_policy.blank_record`, and if even that
raises, the event is dropped and counted -- because the one output that is
never an option is the record the producer submitted.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import TYPE_CHECKING

from zeroth.governance.audit import capture_policy as capture_policy_module
from zeroth.governance.audit.delivery_state import DeliveryFailure, PendingAudit
from zeroth.governance.audit.errors import DuplicateAuditIdError

if TYPE_CHECKING:
    from zeroth.governance.audit.capture_policy import AuditCapturePolicy
    from zeroth.governance.audit.delivery_state import (
        AuditRecordWriter,
        DeliveryCounters,
    )
    from zeroth.governance.audit.models import NodeAuditRecord

logger = logging.getLogger(__name__)


class DeliveryWorker:
    """Consume one bounded queue, capture each event, and write it with retries.

    Args:
        writer: The durable append-only write.
        policy: The stage's own capture transform. Constructed by the queue and
            never supplied by a caller.
        counters: Where every outcome is recorded.
        queue: The bounded hand-off this loop drains.
        max_attempts: Write attempts per event, including the first.
        base_delay: Backoff base for the second attempt onward.
        max_delay: Ceiling the jittered backoff cannot exceed.
    """

    def __init__(
        self,
        *,
        writer: AuditRecordWriter,
        policy: AuditCapturePolicy,
        counters: DeliveryCounters,
        queue: asyncio.Queue[PendingAudit],
        max_attempts: int,
        base_delay: float,
        max_delay: float,
    ) -> None:
        self._writer = writer
        self._policy = policy
        self._counters = counters
        self._queue = queue
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._in_flight: PendingAudit | None = None

    @property
    def in_flight(self) -> PendingAudit | None:
        """The event currently being captured or written, if any."""
        return self._in_flight

    async def run(self) -> None:
        """Deliver queued events one at a time until cancelled."""
        while True:
            item = await self._queue.get()
            self._in_flight = item
            try:
                captured = self._capture(item)
                if captured is None:
                    self.fail(item.audit_id, DeliveryFailure.CAPTURE_FAILED)
                else:
                    await self._deliver(PendingAudit(audit_id=item.audit_id, record=captured))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one event must never take the
                # only worker with it. A crash escaping here left every later
                # event queued forever and uncounted, and re-raised out of the
                # stage's ``aclose`` with the failure recorded nowhere.
                self.fail(item.audit_id, DeliveryFailure.WORKER_ERROR, exc)
            finally:
                self._in_flight = None
                self._queue.task_done()
                self._counters.publish_depth(self._queue.qsize())

    def fail(self, audit_id: str, code: DeliveryFailure, exc: BaseException | None = None) -> None:
        """Count one permanently undelivered event and keep its id for the close report."""
        self._counters.record_failure(audit_id)
        logger.warning(
            "audit delivery failed code=%s audit_id=%s exception_type=%s",
            code.value,
            audit_id,
            "none" if exc is None else type(exc).__name__,
        )

    def _capture(self, item: PendingAudit) -> NodeAuditRecord | None:
        """Apply the stage's own capture policy, degrading to blank, then to nothing.

        Returns:
            The record to write, or ``None`` when even the blank fallback
            failed -- in which case the event is dropped rather than persisted
            as the producer submitted it.
        """
        try:
            return self._policy.apply(item.record)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the policy already fails closed
            # internally, so reaching here means it failed while doing so.
            self._log_degraded(item.audit_id, exc)
        try:
            return capture_policy_module.blank_record(item.record)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the last rung; a record that
            # cannot even be emptied is given up on, counted and named.
            self._log_degraded(item.audit_id, exc)
            return None

    async def _deliver(self, item: PendingAudit) -> None:
        """Write one event, retrying the same ``audit_id`` until attempts run out."""
        attempt = 1
        while True:
            try:
                await self._writer.write(item.record)
            except asyncio.CancelledError:
                raise
            except DuplicateAuditIdError:
                # The append-only contract's one benign refusal: this audit_id is
                # already stored, so an earlier attempt did land. Counting it as
                # failed would report a loss that never happened, and re-writing
                # it is impossible by design. Deliberately narrow -- every other
                # error, plain ``ValueError`` included, means no row was written.
                self._counters.increment("delivered")
                return
            except Exception as exc:  # noqa: BLE001 - the writer is injected, so any
                # non-duplicate failure (locked database, disk, a transport under
                # a different implementation) is transient until the attempt
                # budget says otherwise. Narrowing this would let an
                # unanticipated storage error kill the worker for every later
                # event, which is a much larger loss than one dropped record.
                if attempt >= self._max_attempts:
                    self.fail(item.audit_id, DeliveryFailure.WRITE_FAILED, exc)
                    return
                self._counters.increment("retried")
                await asyncio.sleep(self._backoff_delay(attempt))
                attempt += 1
            else:
                self._counters.increment("delivered")
                return

    def _backoff_delay(self, attempt: int) -> float:
        """Full-jitter exponential backoff for a 1-based attempt number."""
        ceiling = min(self._base_delay * (2 ** (attempt - 1)), self._max_delay)
        return random.uniform(0.0, ceiling)  # noqa: S311 - backoff jitter, not crypto

    def _log_degraded(self, audit_id: str, exc: BaseException) -> None:
        """Note a capture degradation by code and exception type, never by message.

        ``str(exc)`` is attacker-reachable here: the capture transform walks
        producer-supplied payloads and calls an injected classifier, either of
        which can put the value it was holding into the message it raises. The
        log stream is an export path none of the record-level checks cover.
        """
        logger.warning(
            "audit delivery capture degraded code=%s audit_id=%s exception_type=%s",
            DeliveryFailure.CAPTURE_FAILED.value,
            audit_id,
            type(exc).__name__,
        )


__all__ = ["DeliveryWorker"]
