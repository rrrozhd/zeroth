"""What the delivery stage does with an event it cannot deliver.

R8 says a delivery failure is never represented as a successful delivery, and
R3 says the redaction transform runs before any durable write and is enforced
structurally. Both used to be violated by the same kind of mistake -- reading
one exception type as if it meant another -- so the writers and transforms here
are built to raise the *specific* wrong thing: a plain ``ValueError`` that is
not a duplicate id, a capture transform that fails while failing closed, and an
exception whose message carries the value that caused it.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from zeroth.governance.audit import delivery_worker as delivery_module
from zeroth.governance.audit.delivery import AuditDeliveryQueue
from zeroth.governance.audit.errors import DuplicateAuditIdError
from zeroth.governance.audit.models import NodeAuditRecord


class _CollectingAuditWriter:
    """Append-only writer: stores each record, rejects a duplicate audit_id."""

    def __init__(self) -> None:
        self.records: list[NodeAuditRecord] = []
        self.attempted_ids: list[str] = []

    async def write(self, record: NodeAuditRecord) -> NodeAuditRecord:
        self.attempted_ids.append(record.audit_id)
        if any(stored.audit_id == record.audit_id for stored in self.records):
            raise DuplicateAuditIdError(f"audit_id {record.audit_id!r} already exists")
        self.records.append(record)
        return record


class _ValidationRejectingWriter(_CollectingAuditWriter):
    """Raises a plain ``ValueError`` before the commit -- nothing is ever stored."""

    async def write(self, record: NodeAuditRecord) -> NodeAuditRecord:
        self.attempted_ids.append(record.audit_id)
        raise ValueError("record failed pre-commit validation")


class _ExplodingCapturePolicy:
    """Stands in for a capture transform that fails while failing closed."""

    def apply(self, record: NodeAuditRecord) -> NodeAuditRecord:
        raise RuntimeError(f"transform failed on {record.audit_id}")


def _record(audit_id: str, *, tenant_id: str = "tenant-a") -> NodeAuditRecord:
    return NodeAuditRecord(
        audit_id=audit_id,
        run_id="run-1",
        node_id="node-1",
        graph_version_ref="graph:v1",
        deployment_ref="deployment-1",
        tenant_id=tenant_id,
        status="completed",
    )


def _queue(writer: Any, **kwargs: Any) -> AuditDeliveryQueue:
    kwargs.setdefault("base_delay_seconds", 0)
    kwargs.setdefault("max_delay_seconds", 0)
    return AuditDeliveryQueue(writer, **kwargs)


async def test_a_pre_commit_value_error_is_never_reported_as_a_delivered_record() -> None:
    # R8. The writer raises a plain ValueError and stores nothing; reading every
    # ValueError as "already stored" reported delivered=1, failed=0 for a record
    # that does not exist. Only DuplicateAuditIdError means "already durable".
    writer = _ValidationRejectingWriter()
    queue = _queue(writer, max_attempts=2)

    queue.submit(_record("audit-invalid"))
    report = await queue.aclose(timeout=1.0)

    assert writer.records == []
    assert report.counts.delivered == 0
    assert report.counts.failed == 1
    assert report.counts.retried == 1
    assert report.undelivered_audit_ids == ("audit-invalid",)


async def test_a_duplicate_id_error_is_the_only_write_refusal_read_as_delivered() -> None:
    # The other side of the same line: the append-only duplicate check means the
    # record is durable, so it is delivered rather than retried into failure.
    writer = _CollectingAuditWriter()
    queue = _queue(writer, max_attempts=1)

    queue.submit(_record("audit-twice"))
    await queue.aclose(timeout=1.0)
    second = _queue(writer, max_attempts=1)
    second.submit(_record("audit-twice"))
    report = await second.aclose(timeout=1.0)

    assert len(writer.records) == 1
    assert report.counts.delivered == 1
    assert report.counts.failed == 0


async def test_a_capture_that_crashes_does_not_kill_the_worker_or_lose_later_events() -> None:
    # The transform is queue-owned and has no injection point, so the crash is
    # induced directly: an exception escaping it killed the only worker, made
    # aclose re-raise, and left the event counted nowhere.
    writer = _CollectingAuditWriter()
    queue = _queue(writer)
    queue._loop_worker._policy = _ExplodingCapturePolicy()

    queue.submit(_record("audit-crashes"))
    queue.submit(_record("audit-after"))
    report = await queue.aclose(timeout=1.0)

    # Both events reached the writer through the queue's own blank fallback.
    assert [stored.audit_id for stored in writer.records] == ["audit-crashes", "audit-after"]
    assert report.counts.delivered == 2
    assert report.counts.failed == 0
    assert all(stored.input_snapshot == {} for stored in writer.records)


async def test_an_event_whose_blank_fallback_also_fails_is_counted_and_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The last resort: nothing safe can be produced, so the event is dropped as
    # failed -- never persisted as the producer submitted it -- and the worker
    # goes on to the next one.
    def _explode(record: NodeAuditRecord) -> NodeAuditRecord:
        raise RuntimeError(f"cannot blank {record.audit_id}")

    monkeypatch.setattr(delivery_module.capture_policy_module, "blank_record", _explode)
    writer = _CollectingAuditWriter()
    queue = _queue(writer)
    queue._loop_worker._policy = _ExplodingCapturePolicy()

    queue.submit(_record("audit-unblankable"))
    report = await queue.aclose(timeout=1.0)

    assert writer.records == []
    assert report.counts.failed == 1
    assert report.counts.delivered == 0
    assert report.undelivered_audit_ids == ("audit-unblankable",)


async def test_a_write_failure_is_logged_by_code_and_type_never_by_its_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The injected writer's exception text is outside every record-level check,
    # so it must never carry a payload value into the log stream.
    secret = "sk-proj-WRITER-MESSAGE-PROBE"

    class _LeakingWriter:
        async def write(self, record: NodeAuditRecord) -> NodeAuditRecord:
            raise ConnectionError(f"write refused holding {secret}")

    queue = _queue(_LeakingWriter(), max_attempts=1)
    with caplog.at_level(logging.WARNING, logger="zeroth.governance.audit.delivery"):
        queue.submit(_record("audit-leaky"))
        await queue.aclose(timeout=1.0)

    emitted = " ".join(record.getMessage() for record in caplog.records)
    assert secret not in emitted
    assert "write_failed" in emitted
    assert "ConnectionError" in emitted


async def test_a_capture_policy_cannot_be_injected_into_the_delivery_stage() -> None:
    # R3, structurally: the redaction transform is not a constructor parameter,
    # so no caller can substitute a pass-through for it.
    with pytest.raises(TypeError):
        AuditDeliveryQueue(_CollectingAuditWriter(), capture_policy=_ExplodingCapturePolicy())
