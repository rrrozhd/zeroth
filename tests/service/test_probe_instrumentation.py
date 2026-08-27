from __future__ import annotations

import importlib
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from zeroth.econ.plane.capabilities.models import Capability, Implementation
from zeroth.econ.plane.enforcement.models import CostReservation, TenantBudget
from zeroth.econ.plane.instrumentation.models import ExecutionEvent
from zeroth.integrations.memory.embedding_calls import (
    EmbeddingCallBound,
    EmbeddingCallIdentity,
    EmbeddingCallResult,
)


async def test_probe_instrumentation_persists_cost_and_emits_audit_and_regulus(tmp_path) -> None:
    module = importlib.import_module("zeroth.service.probe_instrumentation")
    assert hasattr(module, "PersistentProbeInstrumentation")

    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'probe-cost.db'}", future=True)
    TenantBudget.__table__.create(engine)
    CostReservation.__table__.create(engine)
    Capability.__table__.create(engine)
    Implementation.__table__.create(engine)
    ExecutionEvent.__table__.create(engine)
    sessions = sessionmaker(bind=engine, class_=Session)
    with sessions() as db:
        db.add(TenantBudget(tenant_id="tenant-a", budget_cap_usd=1.0, updated_at=datetime.now()))
        db.commit()

    class Regulus:
        def __init__(self) -> None:
            self.events = []

        def track_execution(self, event) -> None:
            self.events.append(event)

        def track_execution_confirmed(self, event) -> None:
            self.events.append(event)

    class Audit:
        def __init__(self) -> None:
            self.records = []

        async def write(self, record):
            self.records.append(record)
            return record

    regulus = Regulus()
    audit = Audit()
    instrumentation = module.PersistentProbeInstrumentation(
        session_factory=sessions,
        regulus_client=regulus,
        audit_repository=audit,
        deployment_ref="deployment-a",
        graph_version_ref="graph-a@1",
        workspace_id=None,
    )

    await instrumentation.reserve_probe(
        tenant_id="tenant-a",
        campaign_id="campaign-a",
        operation_id="provider-check:model-a",
        run_id="run-a",
        max_cost_usd="0.20",
        run_cap_usd="0.25",
        capability_id="studio.provider_verification",
        implementation_id="model-a",
    )
    evidence = await instrumentation.commit_probe(
        tenant_id="tenant-a",
        campaign_id="campaign-a",
        operation_id="provider-check:model-a",
        run_id="run-a",
        capability_id="studio.provider_verification",
        implementation_id="model-a",
        actual_cost_usd="0.03",
        cost_measurement="estimated",
        provider_request_id="provider-request-a",
        cleanup_status="complete",
        latency_ms=123,
        input_tokens=11,
        output_tokens=2,
    )

    with sessions() as db:
        row = db.query(CostReservation).one()
        assert row.status == "committed"
        assert row.held_cost_usd == Decimal("0.03")
        assert row.campaign_id == "campaign-a"
        assert row.cost_event_id == evidence.cost_event_id
        assert row.provider_request_id == "provider-request-a"
        assert row.cleanup_status == "complete"

    assert len(regulus.events) == 1
    event = regulus.events[0]
    assert event.execution_id == evidence.cost_event_id
    assert event.campaign_id == "campaign-a"
    assert event.operation_id == "provider-check:model-a"
    assert event.provider_request_id == "provider-request-a"
    assert event.cleanup_status == "complete"
    assert event.metadata["campaign_id"] == "campaign-a"
    assert event.metadata["operation_id"] == "provider-check:model-a"
    assert event.metadata["provider_request_id"] == "provider-request-a"
    assert event.metadata["cleanup_status"] == "complete"
    assert len(audit.records) == 1
    record = audit.records[0]
    assert record.cost_event_id == evidence.cost_event_id
    assert record.campaign_id == "campaign-a"
    assert record.execution_metadata["operation_id"] == "provider-check:model-a"
    assert record.execution_metadata["cost_measurement"] == "estimated"


async def test_probe_delivery_rejection_retains_maximum_as_ambiguous(tmp_path) -> None:
    module = importlib.import_module("zeroth.service.probe_instrumentation")
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'probe-rejected.db'}", future=True)
    TenantBudget.__table__.create(engine)
    CostReservation.__table__.create(engine)
    Capability.__table__.create(engine)
    Implementation.__table__.create(engine)
    ExecutionEvent.__table__.create(engine)
    sessions = sessionmaker(bind=engine, class_=Session)
    with sessions() as db:
        db.add(TenantBudget(tenant_id="tenant-a", budget_cap_usd=1.0, updated_at=datetime.now()))
        db.commit()

    class RejectingRegulus:
        def track_execution_confirmed(self, event) -> None:
            del event
            raise RuntimeError("execution identity rejected")

    class Audit:
        async def write(self, record):
            return record

    instrumentation = module.PersistentProbeInstrumentation(
        session_factory=sessions,
        regulus_client=RejectingRegulus(),
        audit_repository=Audit(),
        deployment_ref="deployment-a",
        graph_version_ref="graph-a@1",
        workspace_id=None,
    )
    await instrumentation.reserve_probe(
        tenant_id="tenant-a",
        campaign_id="campaign-a",
        operation_id="provider-check:rejected",
        run_id="run-a",
        max_cost_usd="0.20",
        run_cap_usd="0.25",
        capability_id="studio.provider_verification",
        implementation_id="model-a",
    )

    with pytest.raises(RuntimeError, match="identity rejected"):
        await instrumentation.commit_probe(
            tenant_id="tenant-a",
            campaign_id="campaign-a",
            operation_id="provider-check:rejected",
            run_id="run-a",
            capability_id="studio.provider_verification",
            implementation_id="model-a",
            actual_cost_usd="0.03",
            cost_measurement="estimated",
            provider_request_id="provider-request-a",
            cleanup_status="complete",
            latency_ms=123,
        )

    with sessions() as db:
        row = db.query(CostReservation).one()
        assert row.status == "ambiguous"
        assert row.held_cost_usd == Decimal("0.20")
        assert row.actual_cost_usd is None
        assert row.provider_request_id == "provider-request-a"
        assert row.cleanup_status == "pending_regulus_delivery"


async def test_embedding_instrumentation_reserves_and_commits_provider_usage(tmp_path) -> None:
    module = importlib.import_module("zeroth.service.probe_instrumentation")
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'embedding-cost.db'}", future=True)
    TenantBudget.__table__.create(engine)
    CostReservation.__table__.create(engine)
    Capability.__table__.create(engine)
    Implementation.__table__.create(engine)
    ExecutionEvent.__table__.create(engine)
    sessions = sessionmaker(bind=engine, class_=Session)
    with sessions() as db:
        db.add(TenantBudget(tenant_id="tenant-a", budget_cap_usd=1.0, updated_at=datetime.now()))
        db.commit()

    class Regulus:
        def __init__(self) -> None:
            self.events = []

        def track_execution(self, event) -> None:
            self.events.append(event)

        def track_execution_confirmed(self, event) -> None:
            self.events.append(event)

    class Audit:
        async def write(self, record):
            return record

    class Estimator:
        def estimate(self, model, *, input_tokens, output_tokens):
            del model, output_tokens
            return Decimal(input_tokens) / Decimal("1000000")

    persistent = module.PersistentProbeInstrumentation(
        session_factory=sessions,
        regulus_client=Regulus(),
        audit_repository=Audit(),
        deployment_ref="deployment-a",
        graph_version_ref="graph-a@1",
        workspace_id=None,
    )
    hooks = module.PersistentEmbeddingInstrumentation(
        instrumentation=persistent,
        cost_estimator=Estimator(),
        run_cap_usd=Decimal("0.25"),
    )
    reservation_id = await hooks.reserve(
        EmbeddingCallIdentity(
            tenant_id="tenant-a",
            run_id="run-a",
            node_id="retrieve",
            campaign_id="campaign-a",
            operation="search",
        ),
        EmbeddingCallBound(
            model="openai/text-embedding-3-small",
            input_count=1,
            input_utf8_bytes=100,
        ),
    )
    await hooks.succeed(
        reservation_id,
        EmbeddingCallResult(
            provider_request_id="embedding-request-a",
            usage={"prompt_tokens": 12, "total_tokens": 12},
        ),
    )

    identity = EmbeddingCallIdentity(
        tenant_id="tenant-a",
        run_id="run-a",
        node_id="retrieve",
        campaign_id="campaign-a",
        operation="search",
    )
    settlements = await hooks.consume_call_costs(identity)
    assert len(settlements) == 1
    settlement = settlements[0]
    assert settlement["operation_id"] == reservation_id
    assert str(settlement["cost_event_id"]).startswith("probe_")
    assert settlement["provider_request_id"] == "embedding-request-a"
    assert settlement["estimated_cost_usd"] == Decimal("0.000012")
    assert settlement["cost_measurement"] == "estimated"
    assert settlement["cleanup_status"] == "complete"
    assert await hooks.consume_call_costs(identity) == ()

    with sessions() as db:
        row = db.query(CostReservation).one()
        assert row.operation_id == reservation_id
        assert row.status == "committed"
        assert row.max_cost_usd == Decimal("0.0001")
        assert row.held_cost_usd == Decimal("0.000012")
        assert row.provider_request_id == "embedding-request-a"


async def test_embedding_instrumentation_rejects_zero_unknown_price_before_reservation() -> None:
    module = importlib.import_module("zeroth.service.probe_instrumentation")

    class Persistent:
        called = False

        async def reserve_probe(self, **fields):
            del fields
            self.called = True

    class Estimator:
        def estimate(self, *args, **kwargs):
            del args, kwargs
            return Decimal("0")

    persistent = Persistent()
    hooks = module.PersistentEmbeddingInstrumentation(
        instrumentation=persistent,
        cost_estimator=Estimator(),
        run_cap_usd=Decimal("0.25"),
    )
    with pytest.raises(RuntimeError, match="not calculable"):
        await hooks.reserve(
            EmbeddingCallIdentity(
                tenant_id="tenant-a",
                run_id="run-a",
                node_id="retrieve",
                campaign_id="campaign-a",
                operation="search",
            ),
            EmbeddingCallBound(
                model="unknown/model",
                input_count=1,
                input_utf8_bytes=10,
            ),
        )
    assert persistent.called is False


async def test_reserved_probe_embedding_commits_existing_operation_and_exposes_evidence() -> None:
    module = importlib.import_module("zeroth.service.probe_instrumentation")

    class Persistent:
        def __init__(self) -> None:
            self.commits = []

        async def commit_probe(self, **fields):
            self.commits.append(fields)
            return module.ProbeEvidence(
                "cost-probe", "estimated", fields["provider_request_id"], "complete"
            )

    class Estimator:
        def estimate(self, model, *, input_tokens, output_tokens):
            assert model == "openai/text-embedding-3-small"
            assert output_tokens == 0
            return Decimal(input_tokens) / Decimal("1000000")

    persistent = Persistent()
    hooks = module.ReservedProbeEmbeddingInstrumentation(
        instrumentation=persistent,
        cost_estimator=Estimator(),
        tenant_id="tenant-a",
        campaign_id="campaign-a",
        operation_id="connector-probe-a",
        run_id="run-a",
        capability_id="connector.probe",
        implementation_id="openai/text-embedding-3-small",
    )
    identity = EmbeddingCallIdentity(
        tenant_id="tenant-a",
        run_id="run-a",
        node_id="connector-probe",
        campaign_id="campaign-a",
        operation="write",
    )
    reservation_id = await hooks.reserve(
        identity,
        EmbeddingCallBound(
            model="openai/text-embedding-3-small",
            input_count=1,
            input_utf8_bytes=4,
        ),
    )
    assert reservation_id == "connector-probe-a"
    await hooks.succeed(
        reservation_id,
        EmbeddingCallResult(
            provider_request_id="embedding-request-a",
            usage={"prompt_tokens": 7, "total_tokens": 7},
        ),
    )

    assert hooks.evidence == module.ProbeEvidence(
        "cost-probe", "estimated", "embedding-request-a", "complete"
    )
    assert persistent.commits == [
        {
            "tenant_id": "tenant-a",
            "campaign_id": "campaign-a",
            "operation_id": "connector-probe-a",
            "run_id": "run-a",
            "capability_id": "connector.probe",
            "implementation_id": "openai/text-embedding-3-small",
            "actual_cost_usd": "0.000007",
            "cost_measurement": "estimated",
            "provider_request_id": "embedding-request-a",
            "cleanup_status": "complete",
            "latency_ms": pytest.approx(0, abs=1000),
            "input_tokens": 7,
            "output_tokens": 0,
        }
    ]


async def test_reserved_probe_embedding_is_single_use_and_retains_ambiguous_maximum() -> None:
    module = importlib.import_module("zeroth.service.probe_instrumentation")

    class Persistent:
        def __init__(self) -> None:
            self.ambiguous = []

        async def mark_probe_ambiguous(self, **fields):
            self.ambiguous.append(fields)
            return module.ProbeEvidence("cost-probe", "unmeasured", None, fields["cleanup_status"])

    class Estimator:
        def estimate(self, *args, **kwargs):
            return Decimal("0.00001")

    persistent = Persistent()
    hooks = module.ReservedProbeEmbeddingInstrumentation(
        instrumentation=persistent,
        cost_estimator=Estimator(),
        tenant_id="tenant-a",
        campaign_id="campaign-a",
        operation_id="connector-probe-a",
        run_id="run-a",
        capability_id="connector.probe",
        implementation_id="openai/text-embedding-3-small",
    )
    identity = EmbeddingCallIdentity(
        tenant_id="tenant-a",
        run_id="run-a",
        node_id="connector-probe",
        campaign_id="campaign-a",
        operation="write",
    )
    bound = EmbeddingCallBound(
        model="openai/text-embedding-3-small", input_count=1, input_utf8_bytes=4
    )
    await hooks.reserve(identity, bound)
    with pytest.raises(RuntimeError, match="exactly one"):
        await hooks.reserve(identity, bound)
    await hooks.ambiguous("connector-probe-a", "timeout")
    assert hooks.evidence.cleanup_status == "pending_reconciliation:timeout"
    assert len(persistent.ambiguous) == 1
