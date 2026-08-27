from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from zeroth.platform.signing import EnvHmacSigner
from zeroth.platform.observability.metrics import MetricsCollector
from zeroth.platform.storage.json import to_json_value
from zeroth.service.certifications.models import (
    CertificationState,
    OverrideScope,
    PromotionConflictError,
    PromotionRejectedError,
    ServingArtifactIdentity,
)
from zeroth.service.certifications.receipt import (
    PromotionReceiptPayload,
    sign_promotion_receipt,
)
from zeroth.service.certifications.repository import CertificationRepository
from zeroth.service.certifications.service import CertificationService

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
COMMIT = "1" * 40
IMAGE = "sha256:" + "2" * 64
ARTIFACT = ServingArtifactIdentity(
    target_key="prod/support-agent",
    app_commit=COMMIT,
    image_digest=IMAGE,
)


def _signed_receipt(
    signer: EnvHmacSigner,
    certification_id: str,
    *,
    environments: tuple[str, ...] = ("test", "production"),
    expires_at: datetime | None = None,
):
    payload = PromotionReceiptPayload(
        certification_id=certification_id,
        tenant_id="tenant-a",
        workspace_id="workspace-a",
        app_name="support-agent",
        app_commit=COMMIT,
        zeroth_version="0.23.11",
        image_reference="registry.example/support-agent",
        image_digest=IMAGE,
        source_digest="sha256:" + "3" * 64,
        evidence_digest="sha256:" + "4" * 64,
        report_digest="sha256:" + "5" * 64,
        environments=environments,
        issued_at=NOW - timedelta(minutes=1),
        expires_at=expires_at or NOW + timedelta(hours=1),
    )
    return sign_promotion_receipt(payload, signer)


@pytest.fixture
def signer() -> EnvHmacSigner:
    return EnvHmacSigner(key_id="certifier-1", keys={"certifier-1": b"secret"})


@pytest.fixture
def service(sqlite_db, signer) -> CertificationService:
    return CertificationService(CertificationRepository(sqlite_db), verifier=signer)


@pytest.mark.parametrize(
    ("environments", "state"),
    [
        ((), CertificationState.BUILDABLE),
        (("test",), CertificationState.TEST_DEPLOYABLE),
        (("production",), CertificationState.CERTIFIED),
        (("test", "production"), CertificationState.CERTIFIED),
    ],
)
async def test_registration_exposes_every_pre_promotion_state(
    service, signer, environments, state
) -> None:
    record = await service.register(
        _signed_receipt(signer, "a" * 31 + str(len(environments)), environments=environments),
        actor_id="certifier",
        now=NOW,
    )

    assert record.state is state
    assert (await service.events(record.certification_id, "tenant-a", "workspace-a"))[-1].state is state


async def test_test_receipt_is_deployable_but_not_production_ready(service, signer) -> None:
    record = await service.register(
        _signed_receipt(signer, "b" * 32, environments=("test",)),
        actor_id="certifier",
        now=NOW,
    )

    evaluation = service.evaluate(record, environment="production", now=NOW)

    assert record.state is CertificationState.TEST_DEPLOYABLE
    assert evaluation.production_ready is False
    blockers = {blocker.code: blocker for blocker in evaluation.blockers}
    assert "production_not_promoted" in blockers
    assert "environment_not_certified" in blockers
    assert "production" in blockers["environment_not_certified"].remediation


async def test_valid_production_receipt_is_blocked_until_exact_target_is_promoted(
    service, signer
) -> None:
    record = await service.register(
        _signed_receipt(signer, "9" * 32), actor_id="certifier", now=NOW
    )

    evaluation = service.evaluate(
        record,
        environment="production",
        now=NOW,
        artifact_identity=ARTIFACT,
    )

    assert evaluation.production_ready is False
    assert evaluation.blockers[0].code == "production_not_promoted"


async def test_expiry_can_be_overridden_with_visible_time_bound_scope(service, signer) -> None:
    record = await service.register(
        _signed_receipt(signer, "c" * 32, expires_at=NOW + timedelta(minutes=1)),
        actor_id="certifier",
        now=NOW,
    )
    expired = NOW + timedelta(minutes=2)
    record = await service.promote(
        record.certification_id,
        "tenant-a",
        "workspace-a",
        artifact_identity=ARTIFACT,
        actor_id="operator",
        now=NOW,
    )
    assert (
        service.evaluate(
            record,
            environment="production",
            now=expired,
            artifact_identity=ARTIFACT,
        ).production_ready
        is False
    )

    record = await service.grant_override(
        record.certification_id,
        "tenant-a",
        "workspace-a",
        scopes=(OverrideScope.RECEIPT_EXPIRED,),
        reason="incident recovery",
        expires_at=expired + timedelta(minutes=10),
        actor_id="admin",
        now=expired,
    )

    evaluation = service.evaluate(
        record,
        environment="production",
        now=expired,
        artifact_identity=ARTIFACT,
    )
    assert evaluation.production_ready is True
    assert evaluation.override_active is True
    assert record.override is not None
    assert record.override.reason == "incident recovery"
    after_override = expired + timedelta(minutes=11)
    evaluation = service.evaluate(
        record,
        environment="production",
        now=after_override,
        artifact_identity=ARTIFACT,
    )
    assert evaluation.production_ready is False
    assert evaluation.override_active is False
    assert evaluation.blockers[0].code == "receipt_expired"


async def test_identity_mismatch_revokes_and_cannot_be_overridden(service, signer) -> None:
    record = await service.register(
        _signed_receipt(signer, "d" * 32), actor_id="certifier", now=NOW
    )

    with pytest.raises(PromotionRejectedError, match="commit"):
        await service.promote(
            record.certification_id,
            "tenant-a",
            "workspace-a",
            artifact_identity=ARTIFACT.model_copy(update={"app_commit": "9" * 40}),
            actor_id="operator",
            now=NOW,
        )

    revoked = await service.get(record.certification_id, "tenant-a", "workspace-a")
    assert revoked is not None and revoked.state is CertificationState.REVOKED
    with pytest.raises(PromotionRejectedError, match="revoked"):
        await service.grant_override(
            record.certification_id,
            "tenant-a",
            "workspace-a",
            scopes=(OverrideScope.RECEIPT_EXPIRED,),
            reason="cannot mask identity",
            expires_at=NOW + timedelta(minutes=5),
            actor_id="admin",
            now=NOW,
        )


async def test_image_change_revokes_a_previously_promoted_receipt(service, signer) -> None:
    record = await service.register(
        _signed_receipt(signer, "e" * 32), actor_id="certifier", now=NOW
    )
    await service.promote(
        record.certification_id,
        "tenant-a",
        "workspace-a",
        artifact_identity=ARTIFACT,
        actor_id="operator",
        now=NOW,
    )

    with pytest.raises(PromotionRejectedError, match="image digest"):
        await service.promote(
            record.certification_id,
            "tenant-a",
            "workspace-a",
            artifact_identity=ARTIFACT.model_copy(
                update={"image_digest": "sha256:" + "8" * 64}
            ),
            actor_id="operator",
            now=NOW,
        )

    changed = await service.get(record.certification_id, "tenant-a", "workspace-a")
    assert changed is not None and changed.state is CertificationState.REVOKED


async def test_serving_artifact_replacement_revokes_production_readiness(service, signer) -> None:
    record = await service.register(
        _signed_receipt(signer, "8" * 32), actor_id="certifier", now=NOW
    )
    identity = ServingArtifactIdentity(
        target_key="production/support-agent",
        app_commit=COMMIT,
        image_digest=IMAGE,
    )
    await service.promote(
        record.certification_id,
        "tenant-a",
        "workspace-a",
        artifact_identity=identity,
        actor_id="operator",
        now=NOW,
    )

    replaced = ServingArtifactIdentity(
        target_key=identity.target_key,
        app_commit=COMMIT,
        image_digest="sha256:" + "7" * 64,
    )
    evaluation = await service.production_readiness(
        replaced,
        "tenant-a",
        "workspace-a",
        now=NOW,
    )

    assert evaluation.production_ready is False
    assert evaluation.blockers[0].code == "certification_revoked"
    persisted = await service.get(record.certification_id, "tenant-a", "workspace-a")
    assert persisted is not None and persisted.state is CertificationState.REVOKED


async def test_serving_target_replacement_never_reuses_another_targets_promotion(
    service, signer
) -> None:
    record = await service.register(
        _signed_receipt(signer, "7" * 32), actor_id="certifier", now=NOW
    )
    identity = ServingArtifactIdentity(
        target_key="production/support-agent",
        app_commit=COMMIT,
        image_digest=IMAGE,
    )
    await service.promote(
        record.certification_id,
        "tenant-a",
        "workspace-a",
        artifact_identity=identity,
        actor_id="operator",
        now=NOW,
    )

    replacement = ServingArtifactIdentity(
        target_key="production/attacker-selected-target",
        app_commit=COMMIT,
        image_digest=IMAGE,
    )
    evaluation = await service.production_readiness(
        replacement,
        "tenant-a",
        "workspace-a",
        now=NOW,
    )

    assert evaluation.production_ready is False
    assert evaluation.blockers[0].code == "production_not_promoted"


async def test_same_promotion_is_idempotent_and_competing_receipt_is_atomic(
    service, signer
) -> None:
    first = await service.register(
        _signed_receipt(signer, "f" * 32), actor_id="certifier", now=NOW
    )
    second = await service.register(
        _signed_receipt(signer, "1" * 32), actor_id="certifier", now=NOW
    )
    kwargs = {
        "artifact_identity": ARTIFACT,
        "actor_id": "operator",
        "now": NOW,
    }

    promoted, retry = await asyncio.gather(
        service.promote(first.certification_id, "tenant-a", "workspace-a", **kwargs),
        service.promote(first.certification_id, "tenant-a", "workspace-a", **kwargs),
    )
    assert promoted.state is retry.state is CertificationState.PROMOTED
    events = await service.events(first.certification_id, "tenant-a", "workspace-a")
    assert [event.event_type for event in events].count("promoted") == 1

    with pytest.raises(PromotionConflictError):
        await service.promote(second.certification_id, "tenant-a", "workspace-a", **kwargs)


async def test_event_history_snapshots_promotion_target_and_override_expiry(
    service, signer
) -> None:
    record = await service.register(
        _signed_receipt(signer, "6" * 32), actor_id="certifier", now=NOW
    )
    override_expiry = NOW + timedelta(minutes=15)
    await service.grant_override(
        record.certification_id,
        "tenant-a",
        "workspace-a",
        scopes=(OverrideScope.RECEIPT_EXPIRED,),
        reason="approved recovery window",
        expires_at=override_expiry,
        actor_id="admin",
        now=NOW,
    )
    await service.promote(
        record.certification_id,
        "tenant-a",
        "workspace-a",
        artifact_identity=ARTIFACT,
        actor_id="operator",
        now=NOW,
    )
    await service.revoke(
        record.certification_id,
        "tenant-a",
        "workspace-a",
        reason="artifact replaced",
        actor_id="operator",
        now=NOW + timedelta(minutes=1),
    )

    events = await service.events(record.certification_id, "tenant-a", "workspace-a")
    override_event = next(event for event in events if event.event_type == "override_granted")
    promoted_event = next(event for event in events if event.event_type == "promoted")
    revoked_event = next(event for event in events if event.event_type == "revoked")
    revoked = await service.get(record.certification_id, "tenant-a", "workspace-a")

    assert override_event.override_expires_at == override_expiry
    assert promoted_event.promotion_target_key == ARTIFACT.target_key
    assert revoked_event.promotion_target_key is None
    assert revoked is not None and revoked.promotion_target_key is None


async def test_event_failure_rolls_back_promotion(sqlite_db, signer, monkeypatch) -> None:
    repository = CertificationRepository(sqlite_db)
    service = CertificationService(repository, verifier=signer)
    record = await service.register(
        _signed_receipt(signer, "2" * 32), actor_id="certifier", now=NOW
    )

    def fail_event(*args, **kwargs):
        raise RuntimeError("event store unavailable")

    monkeypatch.setattr(repository, "_event_values", fail_event)
    with pytest.raises(RuntimeError, match="event store unavailable"):
        await service.promote(
            record.certification_id,
            "tenant-a",
            "workspace-a",
            artifact_identity=ARTIFACT,
            actor_id="operator",
            now=NOW,
        )

    restarted = CertificationService(CertificationRepository(sqlite_db), verifier=signer)
    persisted = await restarted.get(record.certification_id, "tenant-a", "workspace-a")
    assert persisted is not None and persisted.state is CertificationState.CERTIFIED
    assert persisted.promotion_target_key is None


async def test_scope_is_invisible_and_verifier_failure_is_closed(service, signer) -> None:
    record = await service.register(
        _signed_receipt(signer, "3" * 32), actor_id="certifier", now=NOW
    )
    assert await service.get(record.certification_id, "tenant-b", "workspace-a") is None

    broken = CertificationService(service.repository, verifier=None)
    loaded = await broken.get(record.certification_id, "tenant-a", "workspace-a")
    assert loaded is not None
    evaluation = broken.evaluate(loaded, environment="production", now=NOW)
    assert evaluation.production_ready is False
    assert evaluation.blockers[0].code == "receipt_verifier_unavailable"


async def test_invalidated_signed_evidence_revokes_and_releases_target(
    sqlite_db, signer
) -> None:
    repository = CertificationRepository(sqlite_db)
    service = CertificationService(repository, verifier=signer)
    record = await service.register(
        _signed_receipt(signer, "4" * 32), actor_id="certifier", now=NOW
    )
    record = await service.promote(
        record.certification_id,
        "tenant-a",
        "workspace-a",
        artifact_identity=ARTIFACT,
        actor_id="operator",
        now=NOW,
    )
    tampered = record.receipt.model_copy(
        update={
            "payload": record.receipt.payload.model_copy(
                update={"image_digest": "sha256:" + "9" * 64}
            )
        }
    )
    async with sqlite_db.transaction(write_lock=True) as connection:
        await connection.execute(
            "UPDATE app_certifications SET receipt_json = ? WHERE certification_id = ?",
            (to_json_value(tampered), record.certification_id),
        )

    invalidated = await service.get(record.certification_id, "tenant-a", "workspace-a")

    assert invalidated is not None and invalidated.state is CertificationState.REVOKED
    assert invalidated.promotion_target_key is None
    assert await repository.get_by_target(
        "prod/support-agent", "tenant-a", "workspace-a"
    ) is None
    events = await service.events(record.certification_id, "tenant-a", "workspace-a")
    assert next(event for event in events if event.event_type == "revoked").actor_id == (
        "system:certification-verifier"
    )


async def test_storage_failure_fails_readiness_closed_with_bounded_metrics(signer) -> None:
    class UnavailableRepository:
        async def get_by_target(self, target_key, tenant_id, workspace_id):
            raise RuntimeError("storage unavailable")

    metrics = MetricsCollector()
    service = CertificationService(UnavailableRepository(), verifier=signer, metrics=metrics)

    evaluation = await service.production_readiness(
        ServingArtifactIdentity(
            target_key="prod/customer-supplied-target",
            app_commit=COMMIT,
            image_digest=IMAGE,
        ),
        "tenant-customer-supplied",
        "workspace-customer-supplied",
        now=NOW,
    )

    assert evaluation.production_ready is False
    assert evaluation.blockers[0].code == "certification_storage_unavailable"
    snapshot = metrics.snapshot()
    assert snapshot["gauges"] == {"zeroth_production_ready": 0.0}
    assert "customer-supplied" not in repr(snapshot)
