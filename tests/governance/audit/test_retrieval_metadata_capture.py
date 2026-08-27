from __future__ import annotations

from zeroth.governance.audit.capture_policy import AuditCapturePolicy
from zeroth.governance.audit.models import NodeAuditRecord


def _captured(value: object) -> NodeAuditRecord:
    return AuditCapturePolicy().apply(
        NodeAuditRecord(
            audit_id="retrieval-audit",
            run_id="retrieval-run",
            node_id="local-retrieval",
            graph_version_ref="retrieval-graph@1",
            deployment_ref="retrieval-deployment",
            tenant_id="tenant-a",
            status="completed",
            execution_metadata={"retrieval_result_count": value},
        )
    )


def test_retrieval_result_count_survives_metadata_only_capture_as_a_number() -> None:
    captured = _captured(0)

    assert captured.execution_metadata["retrieval_result_count"] == 0


def test_retrieval_result_count_refuses_non_numeric_content() -> None:
    captured = _captured("zero results from private document")

    value = captured.execution_metadata["retrieval_result_count"]
    assert isinstance(value, dict)
    assert set(value) == {"hmac_sha256", "schema", "count"}
