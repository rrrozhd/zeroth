"""The durable write is the capture boundary, and nothing gets past it.

R3 says classification and redaction happen before *any* durable write, and R5
says a seeded secret is absent under the default policy. Both were true only of
the delivery stage: :class:`~zeroth.governance.audit.repository.AuditRepository`
is handed straight to the orchestration runtime, the approvals service and the
service API, and every one of them called ``write`` directly. The queue-only
tests passed while production node prompts, results, errors and denials went to
storage exactly as their producer built them.

The boundary now lives on ``write`` itself, which is the one place all of those
paths meet (``write_many`` delegates to it). That raises two questions these
tests answer: whether an already-captured record is transformed a second time --
it must not be, because the second pass would replace the digests standing in
for the first pass's dropped content with digests of ``{}`` -- and whether a
producer can *claim* to have been captured. The proof of capture is a nonce
minted per process and stripped before the write, so it is neither guessable nor
readable back out of an audit row.
"""

from __future__ import annotations

from typing import Any

from zeroth.governance.audit.capture_policy import (
    CAPTURE_METADATA_KEY,
    AuditCapturePolicy,
    CaptureDecision,
)
from zeroth.governance.audit.capture_seal import CAPTURE_SEAL_KEY, is_sealed
from zeroth.governance.audit.models import NodeAuditRecord
from zeroth.governance.audit.repository import AuditRepository
from zeroth.governance.audit.verifier import AuditContinuityVerifier

SECRET = "sk-proj-SEEDED-REPOSITORY-PROBE-4b1e77"
ROOT_KEY = "AKIAIOSFODNN7EXAMPLE"


class _ContentClassifier:
    """The opt-in posture: this deployment retains content."""

    def classify(self, record: NodeAuditRecord) -> str:
        """Answer ``content`` whatever the record holds."""
        del record
        return CaptureDecision.CONTENT.value


def _record(audit_id: str = "audit-1", **overrides: Any) -> NodeAuditRecord:
    fields: dict[str, Any] = {
        "audit_id": audit_id,
        "run_id": "run-1",
        "node_id": "node-1",
        "graph_version_ref": "graph:v1",
        "deployment_ref": "deployment-1",
        "tenant_id": "tenant-a",
        "status": "completed",
    }
    fields.update(overrides)
    return NodeAuditRecord(**fields)


async def test_a_direct_write_is_captured_even_though_no_delivery_stage_was_involved(
    sqlite_db: Any,
) -> None:
    # The probe: the runtime holds the repository and writes prompts through it.
    repository = AuditRepository(sqlite_db)

    await repository.write(
        _record(
            input_snapshot={"prompt": SECRET},
            output_snapshot={"answer": SECRET},
            execution_metadata={"api_key": SECRET},
            error=f"provider rejected {SECRET}",
        )
    )

    stored = await repository.get("audit-1")
    assert stored is not None
    assert SECRET not in stored.model_dump_json()
    assert stored.input_snapshot == {}
    assert stored.output_snapshot == {}


async def test_the_captured_row_still_records_what_it_dropped(sqlite_db: Any) -> None:
    # R4's retention half: the write is content-free, not evidence-free.
    repository = AuditRepository(sqlite_db)

    await repository.write(_record(input_snapshot={"prompt": SECRET}))

    stored = await repository.get("audit-1")
    assert stored is not None
    capture = stored.execution_metadata[CAPTURE_METADATA_KEY]
    assert capture["classification"] == CaptureDecision.METADATA_ONLY.value
    assert capture["content_retained"] is False
    assert len(capture["dropped_fields"]["input_snapshot"]["sha256"]) == 64


async def test_the_process_nonce_is_never_persisted(sqlite_db: Any) -> None:
    # The proof of capture is stripped before the write, so it cannot be read
    # out of an audit row and replayed by whoever can read one.
    repository = AuditRepository(sqlite_db)

    await repository.write(_record())

    stored = await repository.get("audit-1")
    assert stored is not None
    assert CAPTURE_SEAL_KEY not in stored.execution_metadata[CAPTURE_METADATA_KEY]
    assert is_sealed(stored) is False


async def test_a_record_the_delivery_stage_already_captured_is_not_captured_twice(
    sqlite_db: Any,
) -> None:
    # Two passes would summarize the already-emptied channels, replacing the
    # digest of the dropped payload with the digest of ``{}`` -- deleting the
    # only evidence that the content was ever there.
    repository = AuditRepository(sqlite_db)
    captured = AuditCapturePolicy().apply(_record(input_snapshot={"prompt": SECRET}))
    expected = captured.execution_metadata[CAPTURE_METADATA_KEY]["dropped_fields"]

    await repository.write(captured)

    stored = await repository.get("audit-1")
    assert stored is not None
    assert stored.execution_metadata[CAPTURE_METADATA_KEY]["dropped_fields"] == expected


async def test_a_content_classification_made_before_the_write_is_honoured(
    sqlite_db: Any,
) -> None:
    # The seal is not "the queue did it", it is "a policy in this process
    # classified this record" -- which is how a deployment that opts into
    # content capture reaches storage with its content intact.
    repository = AuditRepository(sqlite_db)
    policy = AuditCapturePolicy(classifier=_ContentClassifier())

    await repository.write(policy.apply(_record(input_snapshot={"prompt": "hello"})))

    stored = await repository.get("audit-1")
    assert stored is not None
    assert stored.input_snapshot == {"prompt": "hello"}


async def test_a_producer_cannot_forge_the_marker_to_skip_the_boundary(
    sqlite_db: Any,
) -> None:
    # R3's structural half. ``execution_metadata`` is producer-supplied, so the
    # capture marker's *shape* is forgeable by anyone who has read the module.
    # Only the process nonce is not, and a hand-built marker does not carry it.
    repository = AuditRepository(sqlite_db)
    forged = _record(
        input_snapshot={"prompt": SECRET},
        execution_metadata={
            CAPTURE_METADATA_KEY: {
                "classification": CaptureDecision.CONTENT.value,
                "content_retained": True,
                "dropped_fields": {},
            }
        },
    )

    await repository.write(forged)

    stored = await repository.get("audit-1")
    assert stored is not None
    assert SECRET not in stored.model_dump_json()
    assert stored.input_snapshot == {}
    assert stored.execution_metadata[CAPTURE_METADATA_KEY]["content_retained"] is False


async def test_a_guessed_nonce_value_does_not_pass_the_seal_check(sqlite_db: Any) -> None:
    repository = AuditRepository(sqlite_db)
    forged = _record(
        input_snapshot={"prompt": SECRET},
        execution_metadata={
            CAPTURE_METADATA_KEY: {
                "classification": CaptureDecision.CONTENT.value,
                "content_retained": True,
                CAPTURE_SEAL_KEY: "0" * 32,
            }
        },
    )

    await repository.write(forged)

    stored = await repository.get("audit-1")
    assert stored is not None
    assert stored.input_snapshot == {}
    assert SECRET not in stored.model_dump_json()


async def test_a_seeded_secret_used_as_a_snapshot_key_is_not_persisted(
    sqlite_db: Any,
) -> None:
    # R5 against the durable write: an AWS-style key is a perfectly well-formed
    # identifier, so gating mapping keys on "looks like a name" persisted it.
    repository = AuditRepository(sqlite_db)

    await repository.write(_record(input_snapshot={ROOT_KEY: "value"}))

    stored = await repository.get("audit-1")
    assert stored is not None
    assert ROOT_KEY not in stored.model_dump_json()


async def test_the_digest_chain_verifies_over_captured_records(sqlite_db: Any) -> None:
    # Capture runs before the digest is computed, so the stored bytes and the
    # digest are taken from the same object; the chain a reader recomputes from
    # ``record_json`` is the chain that was written.
    repository = AuditRepository(sqlite_db)
    for index in range(4):
        await repository.write(
            _record(f"audit-{index}", input_snapshot={"prompt": f"{SECRET}-{index}"})
        )

    report = await AuditContinuityVerifier(repository).verify_run("run-1")

    assert report.verified is True
    assert report.record_count == 4


async def test_write_many_is_captured_the_same_way_write_is(sqlite_db: Any) -> None:
    repository = AuditRepository(sqlite_db)

    await repository.write_many(
        [
            _record("audit-a", input_snapshot={"prompt": SECRET}),
            _record("audit-b", output_snapshot={"answer": SECRET}),
        ]
    )

    stored = await repository.list_by_run("run-1")
    assert len(stored) == 2
    assert all(SECRET not in record.model_dump_json() for record in stored)


async def test_the_capture_classifier_is_wiring_and_cannot_be_swapped_twice(
    sqlite_db: Any,
) -> None:
    # A posture that can be changed while the process runs is not a posture.
    repository = AuditRepository(sqlite_db)
    repository.configure_capture(_ContentClassifier())

    try:
        repository.configure_capture(_ContentClassifier())
    except ValueError:
        return
    raise AssertionError("a second capture configuration must be refused")
