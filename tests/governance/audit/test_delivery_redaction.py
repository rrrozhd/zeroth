"""Capture classification and redaction on the audit delivery path.

These tests pin the three properties that make the delivery stage safe to point
at durable storage: the transform runs before any write and cannot be bypassed
(R3), content capture is off by default while the useful metadata survives (R4),
and secrets seeded into prompt-, argument- and result-shaped fields do not reach
the writer (R5).

The writers below record every *attempt*, not just the stored rows, because the
retry and partial-success paths are exactly where an ordering bug would show:
a stage that captured per attempt, or that fell back to the submitted record
after a failure, would still pass a test that only inspected the happy path.
"""

from __future__ import annotations

from typing import Any

import pytest

from zeroth.governance.audit.capture_policy import (
    CAPTURE_METADATA_KEY,
    AuditCapturePolicy,
    CaptureDecision,
)
from zeroth.governance.audit.delivery import AuditDeliveryQueue
from zeroth.governance.audit.models import (
    ApprovalActionRecord,
    MemoryAccessRecord,
    NodeAuditRecord,
    TokenUsage,
    ToolCallRecord,
)

API_KEY = "sk-proj-Zq7Q1f4A2b7D8e0aX9v3TnKp"
BEARER = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.cGF5bG9hZA.c2lnbmF0dXJl"
PASSWORD = "hunter2-correct-horse-battery-staple"
ROOT_KEY = "AKIAIOSFODNN7EXAMPLE"
SEEDED_SECRETS = (API_KEY, BEARER, PASSWORD, ROOT_KEY)


class _RecordingWriter:
    """Append-only writer that keeps every record it was handed, attempt by attempt."""

    def __init__(self) -> None:
        self.records: list[NodeAuditRecord] = []
        self.attempts: list[NodeAuditRecord] = []

    async def write(self, record: NodeAuditRecord) -> NodeAuditRecord:
        self.attempts.append(record)
        if any(stored.audit_id == record.audit_id for stored in self.records):
            raise ValueError(f"audit_id {record.audit_id!r} already exists")
        self.records.append(record)
        return record


class _FlakyRecordingWriter(_RecordingWriter):
    """Fails the first ``failures`` attempts outright, recording each one."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self._remaining = failures

    async def write(self, record: NodeAuditRecord) -> NodeAuditRecord:
        if self._remaining > 0:
            self._remaining -= 1
            self.attempts.append(record)
            raise ConnectionError("audit write failed")
        return await super().write(record)


class _CrashAfterCommitWriter(_RecordingWriter):
    """Stores the row and then raises once -- the partial-success case."""

    def __init__(self) -> None:
        super().__init__()
        self._crashed = False

    async def write(self, record: NodeAuditRecord) -> NodeAuditRecord:
        stored = await super().write(record)
        if not self._crashed:
            self._crashed = True
            raise ConnectionError("connection reset after commit")
        return stored


class _FixedClassifier:
    """Returns whatever it was constructed with, valid classification or not."""

    def __init__(self, decision: object) -> None:
        self._decision = decision

    def classify(self, record: NodeAuditRecord) -> Any:
        del record
        return self._decision


class _ExplodingClassifier:
    """A deployment-supplied classifier that throws."""

    def classify(self, record: NodeAuditRecord) -> str:
        del record
        raise RuntimeError("classifier unavailable")


class _CountingCapturePolicy:
    """Stamps a recognisable record and counts how often it was applied."""

    def __init__(self) -> None:
        self.applied: list[str] = []

    def apply(self, record: NodeAuditRecord) -> NodeAuditRecord:
        self.applied.append(record.audit_id)
        return record.model_copy(update={"input_snapshot": {}, "stdout": "captured"})


class _ExplodingCapturePolicy(AuditCapturePolicy):
    """A policy whose transform blows up part-way through the walk."""

    def _apply(self, record: NodeAuditRecord) -> NodeAuditRecord:
        del record
        raise RuntimeError("transform failed")


def _seeded_record(audit_id: str = "audit-seeded") -> NodeAuditRecord:
    """Build a record with secrets planted at several depths in every content channel."""
    return NodeAuditRecord(
        audit_id=audit_id,
        run_id="run-1",
        node_id="node-1",
        graph_version_ref="graph:v1",
        deployment_ref="deployment-1",
        status="completed",
        input_snapshot={
            "prompt": f"summarise the ledger using {API_KEY}",
            "context": {
                "headers": {"authorization": BEARER},
                "history": [{"turn": 1, "note": {"vault": PASSWORD}}],
            },
        },
        output_snapshot={"answer": {"text": f"done, root key {ROOT_KEY}"}},
        validation_results={"schema": [{"offending_value": PASSWORD}]},
        condition_results=[{"branch": "a", "evidence": {"probe": API_KEY}}],
        execution_metadata={"node_kind": "agent", "duration_ms": 42, "api_key": API_KEY},
        tool_calls=[
            ToolCallRecord(
                tool_ref="tool:http",
                alias="http",
                arguments={"credentials": {"password": PASSWORD}},
                outcome={"body": [BEARER]},
            )
        ],
        memory_interactions=[
            MemoryAccessRecord(
                memory_ref="mem:1",
                connector_type="redis",
                scope="run",
                operation="write",
                key="ledger",
                value={"rows": [{"leaked": ROOT_KEY}]},
            )
        ],
        approval_actions=[
            ApprovalActionRecord(
                approval_id="approval-1",
                action="approved",
                metadata={"password": PASSWORD, "reviewer": "ops"},
            )
        ],
        stdout=f"log line carrying {ROOT_KEY}",
        stderr=f"traceback carrying {BEARER}",
        token_usage=TokenUsage(input_tokens=11, output_tokens=7, total_tokens=18, model_name="m"),
        cost_usd=0.25,
    )


def _queue(writer: Any, **kwargs: Any) -> AuditDeliveryQueue:
    kwargs.setdefault("base_delay_seconds", 0)
    kwargs.setdefault("max_delay_seconds", 0)
    return AuditDeliveryQueue(writer, **kwargs)


async def _deliver_one(writer: Any, record: NodeAuditRecord, **kwargs: Any) -> NodeAuditRecord:
    queue = _queue(writer, **kwargs)
    queue.submit(record)
    await queue.aclose(timeout=1.0)
    return writer.records[0]


async def test_the_writer_never_observes_the_record_object_the_producer_submitted() -> None:
    # R3: the stage transforms; it does not hand the producer's object onward.
    writer = _RecordingWriter()
    submitted = _seeded_record()

    stored = await _deliver_one(writer, submitted)

    assert stored is not submitted
    assert submitted.input_snapshot != {}  # the producer's own copy is untouched


async def test_every_attempt_including_the_retry_writes_only_the_captured_record() -> None:
    # R3: two failed attempts and one that lands -- all three see the policy's output.
    writer = _FlakyRecordingWriter(failures=2)
    queue = _queue(writer, max_attempts=3, capture_policy=_CountingCapturePolicy())

    queue.submit(_seeded_record("audit-retried"))
    await queue.aclose(timeout=1.0)

    assert len(writer.attempts) == 3
    assert [attempt.stdout for attempt in writer.attempts] == ["captured"] * 3
    assert all(attempt.input_snapshot == {} for attempt in writer.attempts)


async def test_the_capture_policy_runs_once_no_matter_how_many_write_attempts() -> None:
    # Re-running the transform per attempt would double-escape an already
    # redacted value and burn the walk again for nothing.
    policy = _CountingCapturePolicy()
    writer = _FlakyRecordingWriter(failures=2)
    queue = _queue(writer, max_attempts=3, capture_policy=policy)

    queue.submit(_seeded_record("audit-retried"))
    await queue.aclose(timeout=1.0)

    assert policy.applied == ["audit-retried"]
    assert len(writer.attempts) == 3


async def test_a_retry_after_a_partial_success_still_writes_the_captured_record() -> None:
    policy = _CountingCapturePolicy()
    writer = _CrashAfterCommitWriter()
    queue = _queue(writer, max_attempts=3, capture_policy=policy)

    queue.submit(_seeded_record("audit-committed"))
    await queue.aclose(timeout=1.0)

    assert policy.applied == ["audit-committed"]
    assert [attempt.stdout for attempt in writer.attempts] == ["captured", "captured"]


async def test_prompt_argument_and_result_values_are_absent_under_the_default_policy() -> None:
    # R4: the removal half.
    stored = await _deliver_one(_RecordingWriter(), _seeded_record())

    assert stored.input_snapshot == {}
    assert stored.output_snapshot == {}
    assert stored.validation_results == {}
    assert stored.condition_results == []
    assert stored.stdout is None
    assert stored.stderr is None
    assert [call.arguments for call in stored.tool_calls] == [{}]
    assert [call.outcome for call in stored.tool_calls] == [None]
    assert [item.value for item in stored.memory_interactions] == [None]


async def test_timing_outcome_and_decision_metadata_survive_the_default_policy() -> None:
    # R4: the retention half. An audit stripped of its evidence is not an audit.
    submitted = _seeded_record()

    stored = await _deliver_one(_RecordingWriter(), submitted)

    assert stored.audit_id == "audit-seeded"
    assert stored.run_id == "run-1"
    assert stored.node_id == "node-1"
    assert stored.status == "completed"
    assert stored.started_at == submitted.started_at
    assert stored.token_usage == submitted.token_usage
    assert stored.cost_usd == 0.25
    assert stored.execution_metadata["node_kind"] == "agent"
    assert stored.execution_metadata["duration_ms"] == 42
    assert stored.execution_metadata["api_key"] == "***REDACTED***"
    assert [call.tool_ref for call in stored.tool_calls] == ["tool:http"]
    assert [item.operation for item in stored.memory_interactions] == ["write"]
    assert [action.action for action in stored.approval_actions] == ["approved"]
    assert [action.metadata["reviewer"] for action in stored.approval_actions] == ["ops"]


async def test_the_digest_schema_and_count_of_every_dropped_channel_are_retained() -> None:
    # R4: what replaces the content still answers "was it the same payload?".
    submitted = _seeded_record()

    stored = await _deliver_one(_RecordingWriter(), submitted)
    capture = stored.execution_metadata[CAPTURE_METADATA_KEY]
    dropped = capture["dropped_fields"]

    assert capture["classification"] == CaptureDecision.METADATA_ONLY.value
    assert capture["content_retained"] is False
    assert set(dropped) == {
        "input_snapshot",
        "output_snapshot",
        "validation_results",
        "condition_results",
        "stdout",
        "stderr",
        "tool_calls",
        "memory_interactions",
    }
    assert len(dropped["input_snapshot"]["sha256"]) == 64
    assert dropped["input_snapshot"]["sha256"] != dropped["output_snapshot"]["sha256"]
    assert dropped["input_snapshot"]["count"] == len(submitted.input_snapshot)
    assert set(dropped["input_snapshot"]["schema"]) == {"prompt", "context"}
    assert dropped["input_snapshot"]["schema"]["prompt"] == "str"
    assert dropped["stdout"]["count"] == len(submitted.stdout or "")
    assert dropped["tool_calls"]["count"] == 1


async def test_seeded_secrets_at_every_nesting_depth_are_absent_from_the_emitted_record() -> None:
    # R5: planted in mappings, in lists, and several levels down each.
    submitted = _seeded_record()
    assert all(secret in submitted.model_dump_json() for secret in SEEDED_SECRETS)

    stored = await _deliver_one(_RecordingWriter(), submitted)

    emitted = stored.model_dump_json()
    for secret in SEEDED_SECRETS:
        assert secret not in emitted


async def test_a_queue_given_no_capture_policy_still_redacts_before_the_write() -> None:
    # The fail-closed default: "nothing was configured" must not mean "emit raw".
    writer = _RecordingWriter()
    queue = AuditDeliveryQueue(writer, base_delay_seconds=0, max_delay_seconds=0)

    queue.submit(_seeded_record())
    await queue.aclose(timeout=1.0)

    stored = writer.records[0]
    assert stored.input_snapshot == {}
    assert all(secret not in stored.model_dump_json() for secret in SEEDED_SECRETS)


async def test_an_explicit_content_classification_retains_content_but_masks_known_secrets() -> None:
    policy = AuditCapturePolicy(
        classifier=_FixedClassifier(CaptureDecision.CONTENT.value),
        known_secrets={"llm_key": API_KEY},
    )

    stored = await _deliver_one(_RecordingWriter(), _seeded_record(), capture_policy=policy)

    assert stored.execution_metadata[CAPTURE_METADATA_KEY]["content_retained"] is True
    assert stored.input_snapshot["prompt"] == "summarise the ledger using [REDACTED:llm_key]"
    assert stored.input_snapshot["context"]["headers"]["authorization"] == "***REDACTED***"
    assert API_KEY not in stored.model_dump_json()


async def test_a_classifier_that_raises_falls_back_to_retaining_no_content() -> None:
    policy = AuditCapturePolicy(classifier=_ExplodingClassifier())

    stored = await _deliver_one(_RecordingWriter(), _seeded_record(), capture_policy=policy)

    assert stored.input_snapshot == {}
    capture = stored.execution_metadata[CAPTURE_METADATA_KEY]
    assert capture["classification"] == CaptureDecision.METADATA_ONLY.value
    assert all(secret not in stored.model_dump_json() for secret in SEEDED_SECRETS)


@pytest.mark.parametrize(
    "decision",
    ["capture_everything", "", "CONTENT", object(), None, b"content"],
)
async def test_an_unrecognised_classification_retains_no_content(decision: object) -> None:
    # The unknown branch and the conservative branch are the same branch.
    policy = AuditCapturePolicy(classifier=_FixedClassifier(decision))

    stored = await _deliver_one(_RecordingWriter(), _seeded_record(), capture_policy=policy)

    assert stored.input_snapshot == {}
    assert stored.execution_metadata[CAPTURE_METADATA_KEY]["content_retained"] is False


async def test_a_policy_that_fails_mid_transform_emits_a_blank_record_not_a_raw_one() -> None:
    # The other fail-closed direction: a broken transform loses the content, not
    # the guarantee.
    stored = await _deliver_one(
        _RecordingWriter(), _seeded_record(), capture_policy=_ExplodingCapturePolicy()
    )

    assert stored.input_snapshot == {}
    assert stored.tool_calls == []
    assert stored.memory_interactions == []
    assert stored.stdout is None
    assert stored.execution_metadata[CAPTURE_METADATA_KEY]["capture_failed"] is True
    assert all(secret not in stored.model_dump_json() for secret in SEEDED_SECRETS)
