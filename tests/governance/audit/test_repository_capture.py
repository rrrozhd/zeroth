"""The durable write is the capture boundary, and nothing gets past it.

R3 says classification and redaction happen before *any* durable write, and R5
says a seeded secret is absent under the default policy. Both were once true
only of the delivery stage, while
:class:`~zeroth.governance.audit.repository.AuditRepository` is handed straight
to the orchestration runtime, the approvals service and the service API, and
every one of them calls ``write`` directly.

The boundary lives on ``write`` itself, which is the one place all of those
paths meet (``write_many`` delegates to it), and it is now the *only* place
capture happens. That is what these tests pin. Capturing here and on the
delivery worker meant the second pass had to recognise the first's work -- or it
would re-summarize channels already emptied -- and the only channel available
for that recognition was producer-supplied ``execution_metadata``, where a
caller could keep a genuine marker while changing the content around it. So
``write`` reads nothing off the record: no marker, no shape, no claim. It
captures, every time.
"""

from __future__ import annotations

from typing import Any

from zeroth.governance.audit.capture_policy import (
    CAPTURE_METADATA_KEY,
    CaptureDecision,
)
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


async def test_no_record_reaches_storage_without_having_been_captured(sqlite_db: Any) -> None:
    # R3, structurally, at the boundary that owns it. This is the guarantee the
    # delivery stage used to carry ("the writer never sees the producer's
    # object"); it moved here when capture collapsed to one point. Drive the
    # durable write directly with a content-bearing, secret-bearing record: the
    # object the producer holds is untouched, and the row is not it.
    repository = AuditRepository(sqlite_db)
    submitted = _record(
        input_snapshot={"prompt": SECRET},
        output_snapshot={"answer": {"nested": [ROOT_KEY]}},
        stdout=f"log line carrying {SECRET}",
    )

    returned = await repository.write(submitted)

    stored = await repository.get("audit-1")
    assert stored is not None
    assert stored is not submitted
    assert returned is not submitted
    # The producer's own copy is never mutated; the row is a captured copy.
    assert submitted.input_snapshot == {"prompt": SECRET}
    assert stored.input_snapshot == {}
    assert stored.output_snapshot == {}
    assert stored.stdout is None
    assert SECRET not in stored.model_dump_json()
    assert ROOT_KEY not in stored.model_dump_json()
    assert stored.execution_metadata[CAPTURE_METADATA_KEY]["content_retained"] is False


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


async def test_a_capture_marker_already_on_the_record_does_not_prevent_capture(
    sqlite_db: Any,
) -> None:
    # The regression that the audit finding named. ``execution_metadata`` is
    # producer-supplied, so a marker claiming "already captured, content
    # retained" is one dict literal away for anyone who has read the module.
    # ``write`` must not read it -- it must capture regardless -- or the live
    # secret sitting beside the marker goes to storage verbatim.
    repository = AuditRepository(sqlite_db)
    forged = _record(
        input_snapshot={"prompt": SECRET},
        output_snapshot={"answer": ROOT_KEY},
        error=f"provider rejected {SECRET}",
        execution_metadata={
            CAPTURE_METADATA_KEY: {
                "classification": CaptureDecision.CONTENT.value,
                "content_retained": True,
                "dropped_fields": {},
                # Shapes from the deleted seal, in case one is still believed.
                "seal": "0" * 32,
            }
        },
    )

    await repository.write(forged)

    stored = await repository.get("audit-1")
    assert stored is not None
    assert stored.input_snapshot == {}
    assert stored.output_snapshot == {}
    assert SECRET not in stored.model_dump_json()
    assert ROOT_KEY not in stored.model_dump_json()
    # The stored marker is this write's own decision, not the producer's claim.
    capture = stored.execution_metadata[CAPTURE_METADATA_KEY]
    assert capture["classification"] == CaptureDecision.METADATA_ONLY.value
    assert capture["content_retained"] is False


async def test_content_is_retained_only_when_this_repository_was_configured_for_it(
    sqlite_db: Any,
) -> None:
    # Classification is the durable sink's, taken from its wiring -- not read
    # off the record, and not inherited from whatever ran upstream.
    repository = AuditRepository(sqlite_db)
    repository.configure_capture(_ContentClassifier())

    await repository.write(_record(input_snapshot={"prompt": "hello"}))

    stored = await repository.get("audit-1")
    assert stored is not None
    assert stored.input_snapshot == {"prompt": "hello"}


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
