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
from zeroth.governance.audit.models import NodeAuditRecord
from zeroth.governance.audit.verifier import compute_chained_record
from zeroth.platform.signing import EnvHmacSigner
from zeroth.service.provider_max_reconciliation import (
    AmbiguousProviderMaxReconciler,
    ProviderMaxReconciliationError,
    ProviderMaxReconciliationRequest,
)


TENANT = "tenant-a"
CAMPAIGN = "campaign-a"
OPERATION = "workflow:run-a:main:call:1:abc"
RUN = "run-a"
DEPLOYMENT = "deployment-a"
COST_EVENT = "probe_0123456789abcdef"
NODE = "main"
CAPABILITY = "zeroth-cap-main"
IMPLEMENTATION = "zeroth-impl-openai-mini"
MODEL = "openai/gpt-4o-mini"
MAXIMUM = Decimal("0.00048435")


class _AuditRepository:
    def __init__(self, records: list[NodeAuditRecord], signer: EnvHmacSigner) -> None:
        self.records = records
        self.signer = signer
        self.writes = 0

    async def list_by_run(self, run_id: str, **scope):
        return [record for record in self.records if record.run_id == run_id]

    async def write(self, record: NodeAuditRecord) -> NodeAuditRecord:
        self.writes += 1
        previous = self.records[-1].record_digest if self.records else None
        chained = compute_chained_record(
            record.model_copy(update={"chain_sequence": len(self.records) + 1}),
            previous,
            self.signer,
        )
        self.records.append(chained)
        return chained


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
    now = datetime.now(UTC).replace(tzinfo=None)
    with sessions() as raw:
        raw.add(TenantBudget(tenant_id=TENANT, budget_cap_usd=1.0, updated_at=now))
        raw.commit()
        from zeroth.econ.plane.scoped_session import ScopedSession
        from zeroth.platform.storage.scoping import TenantWideScopeContext

        scoped = ScopedSession(raw, TenantWideScopeContext(tenant_id=TENANT))
        reserve_cost(
            scoped,
            operation_id=OPERATION,
            max_cost_usd=MAXIMUM,
            campaign_id=CAMPAIGN,
            run_id=RUN,
            deployment_ref=DEPLOYMENT,
            evidence_kind="production",
        )
        mark_cost_ambiguous(
            scoped,
            operation_id=OPERATION,
            cost_event_id=COST_EVENT,
            cleanup_status="pending_reconciliation",
        )
        raw.add(
            ExecutionEvent(
                tenant_id=TENANT,
                campaign_id=CAMPAIGN,
                operation_id=OPERATION,
                deployment_ref=DEPLOYMENT,
                evidence_kind="production",
                provider_request_id=None,
                cleanup_status="pending_reconciliation",
                execution_id=COST_EVENT,
                join_key=RUN,
                timestamp=now,
                capability_id=CAPABILITY,
                implementation_id=IMPLEMENTATION,
                model_version=MODEL,
                token_cost_usd=None,
                tool_cost_usd=None,
                compute_cost_usd=None,
                cost_measurement="unmeasured",
                usage_measurement="unmeasured",
                latency_ms=25,
                compute_time_ms=0,
                event_metadata={
                    "tenant_id": TENANT,
                    "campaign_id": CAMPAIGN,
                    "operation_id": OPERATION,
                    "run_id": RUN,
                    "provider_request_id": None,
                    "cleanup_status": "pending_reconciliation",
                },
            )
        )
        raw.commit()

    signer = EnvHmacSigner(key_id="operator-test", keys={"operator-test": b"secret"})
    audit = compute_chained_record(
        NodeAuditRecord(
            audit_id=f"audit_{COST_EVENT}",
            run_id=RUN,
            node_id=NODE,
            graph_version_ref="graph-a@1",
            deployment_ref=DEPLOYMENT,
            tenant_id=TENANT,
            campaign_id=CAMPAIGN,
            status="ambiguous",
            execution_metadata={
                "campaign_id": CAMPAIGN,
                "operation_id": OPERATION,
                "run_id": RUN,
                "implementation_id": MODEL,
                "cost_measurement": "unmeasured",
                "cleanup_status": "pending_reconciliation",
                "provider_request_id": None,
            },
            cost_usd=None,
            estimated_cost_usd=None,
            cost_measurement="unmeasured",
            cost_event_id=COST_EVENT,
            started_at=now,
            completed_at=now,
        ),
        None,
        signer,
    )
    return sessions, signer, _AuditRepository([audit], signer)


def _request(**changes) -> ProviderMaxReconciliationRequest:
    values = {
        "tenant_id": TENANT,
        "campaign_id": CAMPAIGN,
        "operation_id": OPERATION,
        "run_id": RUN,
        "deployment_ref": DEPLOYMENT,
        "node_id": NODE,
        "capability_id": CAPABILITY,
        "implementation_id": IMPLEMENTATION,
        "model_version": MODEL,
        "cost_event_id": COST_EVENT,
        "held_max_cost_usd": MAXIMUM,
        "actor_sub": "campaign-platform-admin",
        "reason": "provider outcome cannot be measured; settle retained maximum",
    }
    values.update(changes)
    return ProviderMaxReconciliationRequest(**values)


async def test_reconciles_exact_signed_ambiguous_evidence_at_held_maximum_atomically(
    reconciliation_state,
) -> None:
    sessions, signer, audits = reconciliation_state

    result = await AmbiguousProviderMaxReconciler(
        session_factory=sessions,
        audit_repository=audits,
        audit_signer=signer,
    ).reconcile(_request())

    assert result.state == "reconciled"
    assert result.actual_cost_usd == MAXIMUM
    assert result.released_cost_usd == Decimal("0.00000000")
    assert result.provider_request_id is None
    assert result.operator_audit == "appended"
    with sessions() as db:
        reservation = db.query(CostReservation).one()
        assert reservation.status == "committed"
        assert reservation.actual_cost_usd == MAXIMUM
        assert reservation.held_cost_usd == MAXIMUM
        assert reservation.released_cost_usd == Decimal("0.00000000")
        assert reservation.cost_measurement == "estimated"
        assert reservation.cleanup_status == "complete"
        assert reservation.provider_request_id is None
        event = db.query(ExecutionEvent).one()
        assert event.token_cost_usd == MAXIMUM
        assert event.cost_measurement == "estimated"
        assert event.usage_measurement == "unmeasured"
        assert event.cleanup_status == "complete"
        assert event.provider_request_id is None
        assert event.event_metadata["operator_reconciliation"] == "held_maximum"
        log = db.query(AuditLog).one()
        assert log.action == "operator_reconcile_ambiguous_provider_max"
        assert log.actor_sub == "campaign-platform-admin"
        assert log.payload["actual_cost_usd"] == "0.00048435"
    assert audits.records[-1].node_id == "operator.cost_reconciliation"
    assert audits.records[-1].status == "completed"
    assert audits.records[-1].actor is not None
    assert audits.records[-1].actor.roles == ["platform_admin"]
    assert audits.records[-1].record_signature is not None


async def test_retry_is_idempotent_and_does_not_duplicate_event_or_audits(
    reconciliation_state,
) -> None:
    sessions, signer, audits = reconciliation_state
    reconciler = AmbiguousProviderMaxReconciler(
        session_factory=sessions,
        audit_repository=audits,
        audit_signer=signer,
    )
    first = await reconciler.reconcile(_request())
    second = await reconciler.reconcile(_request())

    assert first.state == "reconciled"
    assert second.state == "already_reconciled"
    assert second.operator_audit == "already_present"
    with sessions() as db:
        assert db.query(ExecutionEvent).count() == 1
        assert db.query(AuditLog).count() == 1
    assert audits.writes == 1


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (lambda reservation, event: setattr(reservation, "campaign_id", "other"), "campaign_id"),
        (lambda reservation, event: setattr(reservation, "provider_request_id", "present"), "provider_request_id"),
        (lambda reservation, event: setattr(event, "operation_id", "other"), "operation_id"),
        (lambda reservation, event: setattr(event, "token_cost_usd", Decimal("0.1")), "token_cost_usd"),
    ],
)
async def test_mismatched_or_non_placeholder_durable_evidence_fails_closed(
    reconciliation_state, mutation, match
) -> None:
    sessions, signer, audits = reconciliation_state
    with sessions() as db:
        mutation(db.query(CostReservation).one(), db.query(ExecutionEvent).one())
        db.commit()

    with pytest.raises(ProviderMaxReconciliationError, match=match):
        await AmbiguousProviderMaxReconciler(
            session_factory=sessions,
            audit_repository=audits,
            audit_signer=signer,
        ).reconcile(_request())

    with sessions() as db:
        assert db.query(AuditLog).count() == 0
    assert audits.writes == 0


async def test_unsigned_or_non_ambiguous_source_audit_fails_before_mutation(
    reconciliation_state,
) -> None:
    sessions, signer, audits = reconciliation_state
    audits.records[0] = audits.records[0].model_copy(
        update={"record_signature": None, "signing_key_id": None, "signing_algorithm": None}
    )

    with pytest.raises(ProviderMaxReconciliationError, match="signed audit verification"):
        await AmbiguousProviderMaxReconciler(
            session_factory=sessions,
            audit_repository=audits,
            audit_signer=signer,
        ).reconcile(_request())

    with sessions() as db:
        assert db.query(CostReservation).one().status == "ambiguous"
        assert db.query(ExecutionEvent).one().cost_measurement == "unmeasured"


async def test_wrong_tenant_cannot_select_another_tenants_identity(
    reconciliation_state,
) -> None:
    sessions, signer, audits = reconciliation_state

    with pytest.raises(ProviderMaxReconciliationError, match="exact reservation"):
        await AmbiguousProviderMaxReconciler(
            session_factory=sessions,
            audit_repository=audits,
            audit_signer=signer,
        ).reconcile(_request(tenant_id="tenant-b"))
