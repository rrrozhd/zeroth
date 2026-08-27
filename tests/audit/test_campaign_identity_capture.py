from __future__ import annotations

from zeroth.governance.audit import AuditContinuityVerifier, AuditRepository, NodeAuditRecord
from zeroth.platform.signing import EnvHmacSigner


async def test_typed_campaign_identity_survives_capture_and_signed_chain(sqlite_db) -> None:
    signer = EnvHmacSigner(key_id="campaign-k1", keys={"campaign-k1": b"test-key-material"})
    repository = AuditRepository.for_default_compatibility(sqlite_db, signer=signer)

    written = await repository.write(
        NodeAuditRecord(
            audit_id="rightsizing.call:1",
            run_id="rightsizing:run-1",
            node_id="rightsizing:agent",
            graph_version_ref="graph@1",
            deployment_ref="deployment-1",
            tenant_id="default",
            campaign_id="campaign-1",
            status="completed",
            cost_usd=0.001,
            cost_event_id="cost-1",
            execution_metadata={
                "operation_id": "operation-1",
                "provider_request_id": "provider-raw-must-not-survive",
            },
        )
    )

    stored = await repository.get(written.audit_id)
    assert stored is not None
    assert stored.campaign_id == "campaign-1"
    assert stored.cost_event_id == "cost-1"
    assert stored.record_signature
    assert stored.execution_metadata.get("provider_request_id") is None
    assert "provider-raw-must-not-survive" not in str(stored.execution_metadata)

    report = await AuditContinuityVerifier(repository, signer=signer).verify_run(
        "rightsizing:run-1"
    )
    assert report.verified is True
    assert report.signature_verified is True
    assert report.unsigned_record_count == 0
    assert report.record_count == 1
