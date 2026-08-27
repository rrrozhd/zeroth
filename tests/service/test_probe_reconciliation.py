from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from zeroth.econ.plane.capabilities.models import Capability, Experiment, Implementation
from zeroth.econ.plane.enforcement.models import AuditLog, CostReservation, TenantBudget
from zeroth.econ.plane.enforcement.service import mark_cost_ambiguous, reserve_cost
from zeroth.econ.plane.instrumentation.models import ExecutionEvent
from zeroth.econ.plane.instrumentation.service import ingest_execution
from zeroth.econ.plane.scoped_session import ScopedSession
from zeroth.econ.analytics.identity import capability_identity, implementation_identity
from zeroth.governance.audit.models import NodeAuditRecord, TokenUsage
from zeroth.governance.audit.verifier import compute_chained_record
from zeroth.platform.signing import EnvHmacSigner
from zeroth.platform.storage.scoping import TenantWideScopeContext
from zeroth.service.probe_reconciliation import (
    AmbiguousProbeReconciler,
    ProbeReconciliationError,
    ProbeReconciliationRequest,
)


TENANT = "tenant-a"
CAMPAIGN = "campaign-a"
OPERATION = "provider-check:model-a"
RUN = "run-a"
DEPLOYMENT = "deployment-a"
COST_EVENT = "probe_0123456789abcdef"
PROVIDER_REQUEST = "provider-request-a"
CAPABILITY = "studio.provider_verification"
IMPLEMENTATION = "openai/model-a"


class _AuditRepository:
    def __init__(self, records: list[NodeAuditRecord]) -> None:
        self.records = records

    async def list_by_run(self, run_id: str, **scope):
        return [record for record in self.records if record.run_id == run_id]


class _Regulus:
    def __init__(self, sessions) -> None:
        self.sessions = sessions
        self.deliveries = 0

    def track_execution_confirmed(self, event) -> None:
        self.deliveries += 1
        with self.sessions() as raw:
            ingest_execution(
                ScopedSession(raw, TenantWideScopeContext(tenant_id=TENANT)),
                event,
            )


@pytest.fixture
def reconciliation_state(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'reconcile.db'}", future=True)
    for table in (
        TenantBudget.__table__,
        CostReservation.__table__,
        AuditLog.__table__,
        Capability.__table__,
        Implementation.__table__,
        Experiment.__table__,
        ExecutionEvent.__table__,
    ):
        table.create(engine)
    sessions = sessionmaker(bind=engine, class_=Session)
    with sessions() as raw:
        raw.add(TenantBudget(tenant_id=TENANT, budget_cap_usd=1.0, updated_at=datetime.now()))
        raw.commit()
        scoped = ScopedSession(raw, TenantWideScopeContext(tenant_id=TENANT))
        reserve_cost(
            scoped,
            operation_id=OPERATION,
            max_cost_usd=Decimal("0.20"),
            campaign_id=CAMPAIGN,
            run_id=RUN,
            deployment_ref=DEPLOYMENT,
            evidence_kind="production",
        )
        mark_cost_ambiguous(
            scoped,
            operation_id=OPERATION,
            cost_event_id=COST_EVENT,
            provider_request_id=PROVIDER_REQUEST,
            cleanup_status="pending_regulus_delivery",
        )

    signer = EnvHmacSigner(key_id="operator-test", keys={"operator-test": b"secret"})
    now = datetime.now(UTC)
    audit = compute_chained_record(
        NodeAuditRecord(
            audit_id=f"audit_{COST_EVENT}",
            run_id=RUN,
            node_id=CAPABILITY,
            graph_version_ref="graph-a@1",
            deployment_ref=DEPLOYMENT,
            tenant_id=TENANT,
            campaign_id=CAMPAIGN,
            status="completed",
            execution_metadata={
                "campaign_id": CAMPAIGN,
                "operation_id": OPERATION,
                "run_id": RUN,
                "implementation_id": IMPLEMENTATION,
                "cost_measurement": "estimated",
            },
            token_usage=TokenUsage(
                input_tokens=11,
                output_tokens=2,
                total_tokens=13,
                model_name=IMPLEMENTATION,
            ),
            cost_usd=0.03,
            cost_measurement="estimated",
            cost_event_id=COST_EVENT,
            started_at=now,
            completed_at=now,
        ),
        None,
        signer,
    )
    return sessions, signer, audit


def _request(**changes) -> ProbeReconciliationRequest:
    values = {
        "tenant_id": TENANT,
        "campaign_id": CAMPAIGN,
        "operation_id": OPERATION,
        "run_id": RUN,
        "deployment_ref": DEPLOYMENT,
        "capability_id": CAPABILITY,
        "implementation_id": IMPLEMENTATION,
        "cost_event_id": COST_EVENT,
        "provider_request_id": PROVIDER_REQUEST,
        "held_max_cost_usd": Decimal("0.20"),
        "actual_cost_usd": Decimal("0.03"),
        "cost_measurement": "estimated",
        "actor_sub": "operator@example.test",
    }
    values.update(changes)
    return ProbeReconciliationRequest(**values)


async def test_reconciles_signed_completed_audit_without_replaying_provider(
    reconciliation_state,
) -> None:
    sessions, signer, audit = reconciliation_state
    regulus = _Regulus(sessions)
    result = await AmbiguousProbeReconciler(
        session_factory=sessions,
        regulus_client=regulus,
        audit_repository=_AuditRepository([audit]),
        audit_signer=signer,
    ).reconcile(_request())

    assert result.delivery == "inserted"
    assert regulus.deliveries == 1
    with sessions() as db:
        reservation = db.query(CostReservation).one()
        assert reservation.status == "committed"
        assert reservation.held_cost_usd == Decimal("0.03")
        assert reservation.released_cost_usd == Decimal("0.17")
        event = db.query(ExecutionEvent).one()
        assert event.execution_id == COST_EVENT
        assert event.provider_request_id == PROVIDER_REQUEST
        assert event.token_cost_usd == Decimal("0.03")
        registered_capability = capability_identity(TENANT, DEPLOYMENT, CAPABILITY)
        assert db.query(Capability).one().id == registered_capability
        assert db.query(Implementation).one().id == implementation_identity(
            registered_capability, IMPLEMENTATION
        )
        record = db.query(AuditLog).one()
        assert record.action == "operator_reconcile_probe_delivery"
        assert record.actor_sub == "operator@example.test"
        assert record.payload["released_cost_usd"] == "0.17000000"


async def test_reconciles_metadata_only_captured_audit(reconciliation_state) -> None:
    sessions, signer, audit = reconciliation_state
    captured = compute_chained_record(
        audit.model_copy(
            update={
                "record_digest": "",
                "record_signature": None,
                "signing_key_id": None,
                "signing_algorithm": None,
                "execution_metadata": {
                    "audit_capture": {
                        "classification": "metadata_only",
                        "content_retained": False,
                        "dropped_fields": {
                            "execution_metadata": {
                                "count": 8,
                                "dropped_keys": 8,
                                "hmac_sha256": "a" * 64,
                                "schema": {"<entries>": []},
                            }
                        },
                    }
                },
            }
        ),
        None,
        signer,
    )
    regulus = _Regulus(sessions)

    result = await AmbiguousProbeReconciler(
        session_factory=sessions,
        regulus_client=regulus,
        audit_repository=_AuditRepository([captured]),
        audit_signer=signer,
    ).reconcile(_request())

    assert result.delivery == "inserted"
    assert regulus.deliveries == 1


async def test_existing_exact_execution_is_not_delivered_twice(reconciliation_state) -> None:
    sessions, signer, audit = reconciliation_state
    regulus = _Regulus(sessions)
    reconciler = AmbiguousProbeReconciler(
        session_factory=sessions,
        regulus_client=regulus,
        audit_repository=_AuditRepository([audit]),
        audit_signer=signer,
    )
    await reconciler.deliver_verified_event(_request())
    assert regulus.deliveries == 1

    result = await reconciler.reconcile(_request())

    assert result.delivery == "already_present"
    assert regulus.deliveries == 1
    with sessions() as db:
        assert db.query(ExecutionEvent).count() == 1
        assert db.query(CostReservation).one().status == "committed"


async def test_mismatched_actual_cost_fails_closed_before_delivery(reconciliation_state) -> None:
    sessions, signer, audit = reconciliation_state
    regulus = _Regulus(sessions)
    reconciler = AmbiguousProbeReconciler(
        session_factory=sessions,
        regulus_client=regulus,
        audit_repository=_AuditRepository([audit]),
        audit_signer=signer,
    )

    with pytest.raises(ProbeReconciliationError, match="audit actual cost"):
        await reconciler.reconcile(_request(actual_cost_usd=Decimal("0.04")))

    assert regulus.deliveries == 0
    with sessions() as db:
        assert db.query(CostReservation).one().status == "ambiguous"
        assert db.query(ExecutionEvent).count() == 0
        assert db.query(AuditLog).count() == 0


async def test_unsigned_audit_fails_closed_before_registration(reconciliation_state) -> None:
    sessions, _signer, audit = reconciliation_state
    unsigned = audit.model_copy(
        update={"record_signature": None, "signing_key_id": None, "signing_algorithm": None}
    )
    signer = EnvHmacSigner(key_id="operator-test", keys={"operator-test": b"secret"})
    regulus = _Regulus(sessions)

    with pytest.raises(ProbeReconciliationError, match="signed audit verification"):
        await AmbiguousProbeReconciler(
            session_factory=sessions,
            regulus_client=regulus,
            audit_repository=_AuditRepository([unsigned]),
            audit_signer=signer,
        ).reconcile(_request())

    assert regulus.deliveries == 0
    with sessions() as db:
        assert db.query(Capability).count() == 0
        assert db.query(Implementation).count() == 0


async def test_conflicting_existing_execution_fails_closed_without_redelivery(
    reconciliation_state,
) -> None:
    sessions, signer, audit = reconciliation_state
    regulus = _Regulus(sessions)
    reconciler = AmbiguousProbeReconciler(
        session_factory=sessions,
        regulus_client=regulus,
        audit_repository=_AuditRepository([audit]),
        audit_signer=signer,
    )
    await reconciler.deliver_verified_event(_request())
    with sessions() as db:
        event = db.query(ExecutionEvent).one()
        event.provider_request_id = "different-provider-request"
        db.commit()

    with pytest.raises(ProbeReconciliationError, match="provider_request_id"):
        await reconciler.reconcile(_request())

    assert regulus.deliveries == 1
    with sessions() as db:
        assert db.query(CostReservation).one().status == "ambiguous"
        assert db.query(AuditLog).count() == 0
