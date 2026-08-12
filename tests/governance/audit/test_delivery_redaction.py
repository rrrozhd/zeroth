"""Capture classification and redaction on the path into durable storage.

These tests pin the properties that make an audit record safe to store: the
transform runs before any durable write and cannot be bypassed (R3), content
capture is off by default while the useful metadata survives (R4), and seeded
secrets do not reach storage (R5).

They used to drive the delivery queue, because that is where the transform ran.
It no longer runs there. Capture happened in two places -- the queue's worker
and :meth:`~zeroth.governance.audit.repository.AuditRepository.write` -- and the
second pass had to be told the first had happened, through a marker on
producer-supplied ``execution_metadata`` that a producer could forge. Collapsing
to one capture point moved the guarantee to the durable write, so these tests
moved with it: the queue still appears wherever the *retry* path is what is
under test, but every assertion about what was persisted is made against the
row the repository stored. R3's structural half is unchanged in spirit -- the
transform is not an injectable collaborator anywhere -- only relocated.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from zeroth.governance.audit.capture_policy import (
    CAPTURE_METADATA_KEY,
    AuditCapturePolicy,
    CaptureDecision,
)
from zeroth.governance.audit.capture_projection import (
    canonical_entries,
    canonicalize,
    entry_count,
    key_digest,
)
from zeroth.governance.audit.delivery import AuditDeliveryQueue
from zeroth.governance.audit.models import (
    ApprovalActionRecord,
    MemoryAccessRecord,
    NodeAuditRecord,
    TokenUsage,
    ToolCallRecord,
)
from zeroth.governance.audit.repository import AuditRepository

API_KEY = "sk-proj-Zq7Q1f4A2b7D8e0aX9v3TnKp"
BEARER = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.cGF5bG9hZA.c2lnbmF0dXJl"
PASSWORD = "hunter2-correct-horse-battery-staple"
ROOT_KEY = "AKIAIOSFODNN7EXAMPLE"
SEEDED_SECRETS = (API_KEY, BEARER, PASSWORD, ROOT_KEY)


class _ObservingRepositoryWriter:
    """Passes every write to a real repository, keeping what each attempt was handed."""

    def __init__(self, repository: AuditRepository) -> None:
        self._repository = repository
        self.attempts: list[NodeAuditRecord] = []

    async def write(self, record: NodeAuditRecord) -> NodeAuditRecord:
        self.attempts.append(record)
        return await self._repository.write(record)


class _FlakyRepositoryWriter(_ObservingRepositoryWriter):
    """Fails the first ``failures`` attempts outright, recording each one."""

    def __init__(self, repository: AuditRepository, failures: int) -> None:
        super().__init__(repository)
        self._remaining = failures

    async def write(self, record: NodeAuditRecord) -> NodeAuditRecord:
        if self._remaining > 0:
            self._remaining -= 1
            self.attempts.append(record)
            raise ConnectionError("audit write failed")
        return await super().write(record)


class _CrashAfterCommitRepositoryWriter(_ObservingRepositoryWriter):
    """Commits the row and then raises once -- the partial-success case."""

    _crashed = False

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


class _CountingClassifier:
    """Classifies metadata-only and counts how often the transform consulted it."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def classify(self, record: NodeAuditRecord) -> str:
        self.seen.append(record.audit_id)
        return CaptureDecision.METADATA_ONLY.value


class _ExplodingCapturePolicy(AuditCapturePolicy):
    """A policy whose transform blows up part-way through the walk."""

    def _apply(self, record: NodeAuditRecord) -> NodeAuditRecord:
        del record
        raise RuntimeError("transform failed")


class _PassThroughCapturePolicy:
    """The probe: a "policy" that persists exactly what the producer submitted."""

    def apply(self, record: NodeAuditRecord) -> NodeAuditRecord:
        return record


class _UnsafeKey:
    """A mapping key the projection refuses to print, and refuses to merge with its peers."""

    def __init__(self, secret: str) -> None:
        self._secret = secret

    def __str__(self) -> str:
        return self._secret

    def __repr__(self) -> str:
        return self._secret

    def __hash__(self) -> int:
        return hash(id(self))

    def __eq__(self, other: object) -> bool:
        return self is other


def _seeded_record(audit_id: str = "audit-seeded", **overrides: Any) -> NodeAuditRecord:
    """Build a record with secrets planted at several depths in every content channel."""
    # Keyword form rather than a literal: the call reads as the record's shape.
    fields: dict[str, Any] = dict(
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
    fields.update(overrides)
    return NodeAuditRecord(**fields)


def _repository(sqlite_db: Any, classifier: Any = None) -> AuditRepository:
    """Build a repository, opting into a capture posture only when one is given."""
    repository = AuditRepository(sqlite_db)
    if classifier is not None:
        repository.configure_capture(classifier)
    return repository


async def _store_one(
    sqlite_db: Any, record: NodeAuditRecord, classifier: Any = None
) -> NodeAuditRecord:
    """Write one record through the durable boundary and return the stored row."""
    repository = _repository(sqlite_db, classifier)
    await repository.write(record)
    stored = await repository.get(record.audit_id)
    assert stored is not None
    return stored


def _queue(writer: Any, **kwargs: Any) -> AuditDeliveryQueue:
    kwargs.setdefault("base_delay_seconds", 0)
    kwargs.setdefault("max_delay_seconds", 0)
    return AuditDeliveryQueue(writer, **kwargs)


async def test_a_pass_through_policy_cannot_be_installed_on_the_durable_write(
    sqlite_db: Any,
) -> None:
    # R3, structurally: hand the boundary a policy whose apply() returns its
    # argument and the seeded prompt is persisted verbatim. No parameter, on
    # either the repository or the delivery stage, takes one.
    with pytest.raises(TypeError):
        AuditRepository(sqlite_db, capture_policy=_PassThroughCapturePolicy())
    with pytest.raises(TypeError):
        AuditDeliveryQueue(AuditRepository(sqlite_db), capture_policy=_PassThroughCapturePolicy())

    stored = await _store_one(sqlite_db, _seeded_record())
    assert stored.input_snapshot == {}
    assert all(secret not in stored.model_dump_json() for secret in SEEDED_SECRETS)


async def test_the_stored_row_is_never_the_record_the_producer_submitted(sqlite_db: Any) -> None:
    # R3: the boundary transforms; it does not persist the producer's object.
    submitted = _seeded_record()

    stored = await _store_one(sqlite_db, submitted)

    assert stored is not submitted
    assert submitted.input_snapshot != {}  # the producer's own copy is untouched


async def test_every_attempt_including_the_retry_stores_only_the_captured_record(
    sqlite_db: Any,
) -> None:
    # R3 across the retry path: the delivery stage hands the producer's record
    # to the writer on every attempt, and the writer captures every time. What
    # lands is captured however many attempts it took to land it.
    repository = _repository(sqlite_db)
    writer = _FlakyRepositoryWriter(repository, failures=2)
    queue = _queue(writer, max_attempts=3)

    queue.submit(_seeded_record("audit-retried"))
    await queue.aclose(timeout=1.0)

    assert len(writer.attempts) == 3
    stored = await repository.get("audit-retried")
    assert stored is not None
    assert stored.input_snapshot == {}
    assert all(secret not in stored.model_dump_json() for secret in SEEDED_SECRETS)


async def test_the_capture_policy_runs_once_per_attempt_and_always_on_the_original(
    sqlite_db: Any,
) -> None:
    # The delivery stage no longer mutates the queued record, so a retry starts
    # from the producer's original rather than from an already-captured copy:
    # re-running the transform cannot double-escape an already redacted value.
    classifier = _CountingClassifier()
    repository = _repository(sqlite_db, classifier=classifier)
    writer = _FlakyRepositoryWriter(repository, failures=2)
    queue = _queue(writer, max_attempts=3)

    queue.submit(_seeded_record("audit-retried"))
    await queue.aclose(timeout=1.0)

    assert len(writer.attempts) == 3
    # Only the attempt that reached the durable write consulted the classifier.
    assert classifier.seen == ["audit-retried"]
    assert all(attempt.input_snapshot != {} for attempt in writer.attempts)
    stored = await repository.get("audit-retried")
    assert stored is not None
    assert stored.input_snapshot == {}


async def test_a_retry_after_a_partial_success_still_stores_the_captured_record(
    sqlite_db: Any,
) -> None:
    repository = _repository(sqlite_db)
    writer = _CrashAfterCommitRepositoryWriter(repository)
    queue = _queue(writer, max_attempts=3)

    queue.submit(_seeded_record("audit-committed"))
    await queue.aclose(timeout=1.0)

    stored = await repository.get("audit-committed")
    assert stored is not None
    assert stored.input_snapshot == {}
    assert all(secret not in stored.model_dump_json() for secret in SEEDED_SECRETS)


async def test_prompt_argument_and_result_values_are_absent_under_the_default_policy(
    sqlite_db: Any,
) -> None:
    # R4: the removal half.
    stored = await _store_one(sqlite_db, _seeded_record())

    assert stored.input_snapshot == {}
    assert stored.output_snapshot == {}
    assert stored.validation_results == {}
    assert stored.condition_results == []
    assert stored.stdout is None
    assert stored.stderr is None
    assert [call.arguments for call in stored.tool_calls] == [{}]
    assert [call.outcome for call in stored.tool_calls] == [None]
    assert [item.value for item in stored.memory_interactions] == [None]


async def test_timing_outcome_and_decision_metadata_survive_the_default_policy(
    sqlite_db: Any,
) -> None:
    # R4: the retention half. An audit stripped of its evidence is not an audit.
    submitted = _seeded_record()

    stored = await _store_one(sqlite_db, submitted)

    assert stored.audit_id == "audit-seeded"
    assert stored.run_id == "run-1"
    assert stored.node_id == "node-1"
    assert stored.status == "completed"
    assert stored.started_at == submitted.started_at
    assert stored.token_usage == submitted.token_usage
    assert stored.cost_usd == 0.25
    assert stored.execution_metadata["node_kind"] == "agent"
    assert stored.execution_metadata["duration_ms"] == 42
    assert [call.tool_ref for call in stored.tool_calls] == ["tool:http"]
    assert [item.operation for item in stored.memory_interactions] == ["write"]
    assert [action.action for action in stored.approval_actions] == ["approved"]
    # ``reviewer`` names whoever submitted the approval, so it is retained as a
    # stable digest rather than as their text -- correlatable, not readable.
    [reviewer] = [action.metadata["reviewer"] for action in stored.approval_actions]
    assert len(reviewer["hmac_sha256"]) == 64
    assert reviewer["hmac_sha256"] != hashlib.sha256(b'"ops"').hexdigest()


async def test_the_digest_schema_and_count_of_every_dropped_channel_are_retained(
    sqlite_db: Any,
) -> None:
    # R4: what replaces the content still answers "was it the same payload?".
    # ``validation_results`` carries the collision case: two entries whose keys
    # are both unprintable, so both canonicalize to the same marker.
    submitted = _seeded_record(
        validation_results={"schema": {_UnsafeKey(API_KEY): 1, _UnsafeKey(PASSWORD): "two"}}
    )

    stored = await _store_one(sqlite_db, submitted)
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
        "approval_actions",
        "execution_metadata",
        "error",
    }
    assert len(dropped["input_snapshot"]["hmac_sha256"]) == 64
    assert (
        dropped["input_snapshot"]["hmac_sha256"]
        != dropped["output_snapshot"]["hmac_sha256"]
    )
    assert dropped["input_snapshot"]["count"] == len(submitted.input_snapshot)
    # Key names are hashed, never printed: a credential used as a mapping key
    # would otherwise be persisted verbatim inside the schema of what was
    # dropped. A canonical mapping is an entry *sequence*, so a hashed key is a
    # list element rather than a dict key.
    entries = canonical_entries(dropped["input_snapshot"]["schema"])
    assert entries is not None
    assert sorted(key for key, _value in entries) == sorted(
        (key_digest("prompt"), key_digest("context"))
    )
    assert dict(entries)[key_digest("prompt")] == "str"
    assert dropped["stdout"]["count"] == len(submitted.stdout or "")
    assert dropped["tool_calls"]["count"] == 1
    # The collision the entry sequence exists to remove: both keys render to the
    # same marker, so as dict keys the second overwrote the first and the
    # retained schema and count described a payload with one entry instead of
    # two. R4's retained counts have to hold for keys the projection refuses to
    # print, not only for the ones it can hash.
    outer = canonical_entries(dropped["validation_results"]["schema"])
    assert outer is not None
    assert [key for key, _value in outer] == [key_digest("schema")]
    assert sorted(canonical_entries(outer[0][1]) or []) == [
        ["<key:other>", "int"],
        ["<key:other>", "str"],
    ]
    assert entry_count(canonicalize(submitted.validation_results["schema"])) == 2


async def test_seeded_secrets_at_every_nesting_depth_are_absent_from_the_stored_record(
    sqlite_db: Any,
) -> None:
    # R5: planted in mappings, in lists, and several levels down each.
    submitted = _seeded_record()
    assert all(secret in submitted.model_dump_json() for secret in SEEDED_SECRETS)

    stored = await _store_one(sqlite_db, submitted)

    emitted = stored.model_dump_json()
    for secret in SEEDED_SECRETS:
        assert secret not in emitted


async def test_a_repository_given_no_capture_configuration_still_redacts_before_the_write(
    sqlite_db: Any,
) -> None:
    # The fail-closed default: "nothing was configured" must not mean "store raw".
    repository = AuditRepository(sqlite_db)

    await repository.write(_seeded_record())

    stored = await repository.get("audit-seeded")
    assert stored is not None
    assert stored.input_snapshot == {}
    assert all(secret not in stored.model_dump_json() for secret in SEEDED_SECRETS)


async def test_an_explicit_content_classification_retains_content_through_the_write(
    sqlite_db: Any,
) -> None:
    stored = await _store_one(
        sqlite_db,
        _seeded_record(),
        classifier=_FixedClassifier(CaptureDecision.CONTENT.value),
    )

    assert stored.execution_metadata[CAPTURE_METADATA_KEY]["content_retained"] is True
    assert stored.input_snapshot["prompt"] == f"summarise the ledger using {API_KEY}"
    assert stored.input_snapshot["context"]["headers"]["authorization"] == "***REDACTED***"


async def test_a_content_classification_still_masks_registered_secret_values() -> None:
    # ``known_secrets`` is a policy input, exercised at the policy: the durable
    # write constructs its own policy and ``configure_capture`` takes only a
    # classifier, so no wiring path reaches this today. It is a complement to
    # the channel drop, never the guarantee -- see ``capture_scrub``.
    policy = AuditCapturePolicy(
        classifier=_FixedClassifier(CaptureDecision.CONTENT.value),
        known_secrets={"llm_key": API_KEY},
    )

    captured = policy.apply(_seeded_record())

    assert captured.input_snapshot["prompt"] == "summarise the ledger using [REDACTED:llm_key]"
    assert API_KEY not in captured.model_dump_json()


async def test_a_classifier_that_raises_falls_back_to_retaining_no_content(
    sqlite_db: Any,
) -> None:
    stored = await _store_one(sqlite_db, _seeded_record(), classifier=_ExplodingClassifier())

    assert stored.input_snapshot == {}
    capture = stored.execution_metadata[CAPTURE_METADATA_KEY]
    assert capture["classification"] == CaptureDecision.METADATA_ONLY.value
    assert all(secret not in stored.model_dump_json() for secret in SEEDED_SECRETS)


@pytest.mark.parametrize(
    "decision",
    ["capture_everything", "", "CONTENT", object(), None, b"content"],
)
async def test_an_unrecognised_classification_retains_no_content(
    sqlite_db: Any, decision: object
) -> None:
    # The unknown branch and the conservative branch are the same branch.
    stored = await _store_one(sqlite_db, _seeded_record(), classifier=_FixedClassifier(decision))

    assert stored.input_snapshot == {}
    assert stored.execution_metadata[CAPTURE_METADATA_KEY]["content_retained"] is False


async def test_a_policy_that_fails_mid_transform_emits_a_blank_record_not_a_raw_one() -> None:
    # The other fail-closed direction: a broken transform loses the content, not
    # the guarantee. Exercised at the policy: the boundary has no seam.
    blanked = _ExplodingCapturePolicy().apply(_seeded_record())

    assert blanked.input_snapshot == {}
    assert blanked.tool_calls == []
    assert blanked.memory_interactions == []
    assert blanked.stdout is None
    assert blanked.execution_metadata[CAPTURE_METADATA_KEY]["capture_failed"] is True
    assert all(secret not in blanked.model_dump_json() for secret in SEEDED_SECRETS)


async def test_the_delivery_stage_hands_its_writer_the_record_the_producer_submitted() -> None:
    # The trade-off, stated as a test rather than only as prose: this stage
    # classifies nothing, so its writer is trusted to be a capture-applying
    # durable sink. Production injects only ``AuditRepository``.
    class _Recorder:
        def __init__(self) -> None:
            self.records: list[NodeAuditRecord] = []

        async def write(self, record: NodeAuditRecord) -> NodeAuditRecord:
            self.records.append(record)
            return record

    writer = _Recorder()
    queue = _queue(writer)
    submitted = _seeded_record()

    queue.submit(submitted)
    await queue.aclose(timeout=1.0)

    assert writer.records == [submitted]
