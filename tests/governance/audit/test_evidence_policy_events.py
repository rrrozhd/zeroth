"""What an evidence bundle can still say about denials after the capture boundary.

R4 made ``AuditRepository.write`` an unconditional capture point, and under the
default policy that replaces every ``NodeAuditRecord.error`` with a fixed
redaction marker. :func:`collect_policy_events` matched ``denied`` /
``forbidden`` / ``policy`` against exactly that field, so both ``/evidence``
endpoints returned an empty ``policy_events`` list for every deployment --
silently, because no test covered the pairing of the two changes.

These probes therefore run a *real* capture and then ask for the events, so a
producer that stops promoting its verdict onto the allowlisted structural keys
fails here rather than in production. The last one covers the same failure in
the other direction: retention's econ sweep matches audits by ``join_key``, and
a ``join_key`` the projection strips is a run whose econ events can never be
erased.
"""

from __future__ import annotations

from zeroth.governance.audit.capture_policy import AuditCapturePolicy
from zeroth.governance.audit.capture_projection import REDACTED
from zeroth.governance.audit.evidence import collect_policy_events
from zeroth.governance.audit.models import NodeAuditRecord

DENIAL_PROSE = "capability denied: secret_access by policy"
JOIN_KEY = "case-42:tenant-a"
BRANCH_ID = "9f31c7aa4b0e4d1c8f2a6b5c3d7e8f90:branch:2"


def _record(**overrides: object) -> NodeAuditRecord:
    fields: dict[str, object] = {
        "audit_id": "audit-evidence",
        "run_id": "run-1",
        "node_id": "node-1",
        "graph_version_ref": "graph:v1",
        "deployment_ref": "deployment-1",
        "tenant_id": "tenant-a",
        "status": "completed",
    }
    fields.update(overrides)
    return NodeAuditRecord(**fields)  # type: ignore[arg-type]


def _captured(**overrides: object) -> NodeAuditRecord:
    return AuditCapturePolicy().apply(_record(**overrides))


def test_a_policy_denial_is_reported_after_the_capture_that_redacts_its_error() -> None:
    captured = _captured(
        status="rejected",
        error=DENIAL_PROSE,
        execution_metadata={
            "decision": "deny",
            "reason_code": "capability_denied",
            "enforcement_applied": False,
        },
    )

    # The channel the old lookup read is gone; the verdict is not.
    assert captured.error == REDACTED
    assert collect_policy_events([captured]) == ["node-1 deny: capability_denied"]


def test_an_admission_denial_is_reported_from_its_boolean_verdict_alone() -> None:
    # The executable-unit path files ``admitted``, not ``decision``: a denial
    # must not depend on one producer's choice of field.
    captured = _captured(
        status="rejected",
        execution_metadata={"admitted": False, "reason_code": "runtime_not_allowed"},
    )

    assert collect_policy_events([captured]) == ["node-1 rejected: runtime_not_allowed"]


def test_free_form_error_prose_no_longer_decides_whether_a_record_is_a_denial() -> None:
    # Both halves of the old bug: prose that reads like a denial is not one, and
    # the record is not saved by the fact that its text happens to say "policy".
    completed = _record(status="completed", error=DENIAL_PROSE)

    assert collect_policy_events([completed]) == []
    assert collect_policy_events([AuditCapturePolicy().apply(completed)]) == []


def test_a_run_of_ordinary_records_produces_no_policy_events() -> None:
    captured = _captured(execution_metadata={"node_kind": "agent", "status": "completed"})

    assert collect_policy_events([captured]) == []


def test_the_join_key_a_retention_sweep_matches_on_survives_the_default_capture() -> None:
    # RetentionErasureService reads execution_metadata["join_key"] to find the
    # econ events a run produced. Hashing it would satisfy a presence check and
    # still match nothing, so the projection must keep it verbatim.
    captured = _captured(execution_metadata={"join_key": JOIN_KEY})

    assert captured.execution_metadata["join_key"] == JOIN_KEY


def test_branch_identifiers_survive_the_default_capture_verbatim() -> None:
    # The fan-out audits are correlated by comparing these to the parent run's
    # own ids, so a digest in their place breaks every consumer.
    captured = _captured(execution_metadata={"branch_id": BRANCH_ID, "branch_index": 2})

    assert captured.execution_metadata["branch_id"] == BRANCH_ID
    assert captured.execution_metadata["branch_index"] == 2
