from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from release.live_evaluation.config import CampaignConfig
from release.live_evaluation.control_gate import (
    ChromaCorpusDocument,
    ChromaIdentity,
    ControlPlaneGate,
    PaidProbeAuthorization,
    PaidProbeResult,
    SignedAuditReadiness,
)
from release.live_evaluation.control_gate_runtime import (
    ControlGateRuntime,
    SyntheticChromaDocument,
)
from release.live_evaluation.criteria import original_acceptance_criteria
from release.live_evaluation.evidence import EvidenceStore
from release.live_evaluation.ledger import CampaignLedger
from zeroth.econ.plane.enforcement.models import CostReservation, TenantBudget
from zeroth.econ.plane.instrumentation.models import ExecutionEvent


def _campaign(tmp_path: Path) -> CampaignConfig:
    return CampaignConfig.model_validate(
        {
            "schema_version": 1,
            "campaign_id": "evaluation-control-runtime",
            "tenant_id": "evaluation-control-runtime",
            "provider": "openai",
            "model": "openai/gpt-4o-mini",
            "embedding_model": "openai/text-embedding-3-small",
            "vector_backend": "chroma",
            "campaign_budget_usd": "10.00",
            "per_run_cap_usd": "0.25",
            "provider_secret_ref": "llm.openai",
            "artifact_root": str(tmp_path / "artifacts"),
            "action_sink_root": str(tmp_path / "artifacts" / "action-sink"),
        }
    )


def _runtime(tmp_path: Path):
    database = tmp_path / "econ.db"
    engine = create_engine(
        f"sqlite+pysqlite:///{database}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    for table in (TenantBudget.__table__, ExecutionEvent.__table__, CostReservation.__table__):
        table.create(engine)
    engine.dispose()
    campaign = _campaign(tmp_path)
    store = EvidenceStore(tmp_path / "evidence")
    ledger = CampaignLedger(store, original_acceptance_criteria())
    gate = ControlPlaneGate(store=store, ledger=ledger, campaign=campaign)
    return ControlGateRuntime(
        gate=gate,
        econ_database=database,
        command_working_directory=tmp_path,
    ), store, ledger, campaign, database


class _AuditInspector:
    def inspect(self) -> SignedAuditReadiness:
        return SignedAuditReadiness(
            state="signed",
            algorithm="hmac-sha256",
            signing_reference="evaluation.control.signing",
            evidence_reference=self.evidence_reference,
        )


class _ChromaInspector:
    def inspect(self) -> ChromaIdentity:
        return ChromaIdentity(
            image="chromadb/chroma:1.5.6",
            host="127.0.0.1",
            port=8121,
            instance_id="zeroth-evaluation-chroma",
            api_version="1.5.6",
            evidence_reference=self.evidence_reference,
        )


class _Seeder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[SyntheticChromaDocument, ...]]] = []

    def seed_exactly(
        self, tenant_id: str, documents: tuple[SyntheticChromaDocument, ...]
    ) -> tuple[ChromaCorpusDocument, ...]:
        self.calls.append((tenant_id, documents))
        return tuple(
            ChromaCorpusDocument(item.document_id, tenant_id, item.sha256)
            for item in documents
        )


class _Probe:
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.calls: list[PaidProbeAuthorization] = []

    def execute_paid_probe(self, authorization: PaidProbeAuthorization) -> PaidProbeResult:
        self.calls.append(authorization)
        suffix = authorization.kind
        return PaidProbeResult(
            kind=authorization.kind,
            operation_id=authorization.operation_id,
            run_id=authorization.run_id,
            audit_event_id=f"audit-{suffix}",
            cost_event_id=f"cost-{suffix}",
            provider_request_id=f"provider-request-{suffix}",
            connector_request_id=(f"connector-request-{suffix}" if suffix == "chroma" else None),
            request_count=1,
            cache_hit=False,
            audit_chain_signed=True,
            cleanup_state="committed",
            measured_cost_usd=Decimal("0.0001"),
        )


def _local_inspectors(store: EvidenceStore):
    audit = _AuditInspector()
    audit.evidence_reference = f"events.ndjson#{store.append_event('control.local-proof', {'name': 'audit'})}"
    chroma = _ChromaInspector()
    chroma.evidence_reference = f"events.ndjson#{store.append_event('control.local-proof', {'name': 'chroma'})}"
    return audit, chroma


def test_runtime_derives_local_gate_proofs_from_persistent_services(tmp_path: Path) -> None:
    runtime, store, ledger, campaign, database = _runtime(tmp_path)
    audit, chroma = _local_inspectors(store)
    seeder = _Seeder()

    result = runtime.execute_local_gates(
        audit_inspector=audit,
        chroma_inspector=chroma,
        chroma_seeder=seeder,
    )

    assert result.budget.concurrent_admission_proved is True
    assert result.budget.over_limit_rejected is True
    assert result.budget.commit_and_release_proved is True
    assert result.budget.restart_recovery_proved is True
    assert len(seeder.calls) == 1
    tenant, documents = seeder.calls[0]
    assert tenant == campaign.tenant_id
    assert len(documents) == 3
    assert len({item.document_id for item in documents}) == 3
    assert len({item.sha256 for item in result.documents}) == 3

    engine = create_engine(f"sqlite+pysqlite:///{database}")
    with Session(engine) as session:
        budget = session.execute(select(TenantBudget)).scalar_one()
        reservations = session.execute(select(CostReservation)).scalars().all()
    engine.dispose()
    assert Decimal(str(budget.budget_cap_usd)) == Decimal("10.0")
    assert {item.status for item in reservations} == {"committed", "released"}
    assert sum(Decimal(item.held_cost_usd) for item in reservations) == Decimal("0.01")

    statuses = {item.criterion_id: item.status for item in ledger.criteria}
    assert all(statuses[item] == "pass" for item in (
        "control.tenant-budget-10",
        "control.run-budget-025",
        "control.budget-concurrency",
        "control.budget-rejection",
        "control.budget-commit-release",
        "control.budget-recovery",
        "control.audit-signed",
        "control.chroma-pinned-loopback",
        "control.chroma-corpus-seeded",
    ))
    assert len(list((store.root / "commands").glob("*.json"))) >= 4
    store.scan_recursive()


def test_runtime_resumes_completed_synthetic_budget_proof_without_readmission(
    tmp_path: Path,
) -> None:
    runtime, store, _ledger, campaign, database = _runtime(tmp_path)
    audit, chroma = _local_inspectors(store)
    runtime.execute_local_gates(
        audit_inspector=audit,
        chroma_inspector=chroma,
        chroma_seeder=_Seeder(),
    )

    resumed_store = EvidenceStore(tmp_path / "resumed-evidence")
    resumed_ledger = CampaignLedger(resumed_store, original_acceptance_criteria())
    resumed_gate = ControlPlaneGate(
        store=resumed_store,
        ledger=resumed_ledger,
        campaign=campaign,
    )
    resumed_runtime = ControlGateRuntime(
        gate=resumed_gate,
        econ_database=database,
        command_working_directory=tmp_path,
    )
    resumed_audit, resumed_chroma = _local_inspectors(resumed_store)

    result = resumed_runtime.execute_local_gates(
        audit_inspector=resumed_audit,
        chroma_inspector=resumed_chroma,
        chroma_seeder=_Seeder(),
    )

    assert result.budget.restart_recovery_proved is True
    engine = create_engine(f"sqlite+pysqlite:///{database}")
    with Session(engine) as session:
        assert len(session.execute(select(CostReservation)).scalars().all()) == 4
    engine.dispose()


def test_runtime_executes_each_authorized_probe_exactly_once_and_reconciles(tmp_path: Path) -> None:
    runtime, store, ledger, _campaign_config, _database = _runtime(tmp_path)
    audit, chroma = _local_inspectors(store)
    runtime.execute_local_gates(
        audit_inspector=audit,
        chroma_inspector=chroma,
        chroma_seeder=_Seeder(),
    )
    provider = _Probe("provider")
    chroma_probe = _Probe("chroma")

    results = runtime.execute_authorized_probes(
        provider_probe=provider,
        chroma_probe=chroma_probe,
    )

    assert [item.kind for item in results] == ["provider", "chroma"]
    assert len(provider.calls) == len(chroma_probe.calls) == 1
    assert len({call.authorization_event_id for call in provider.calls + chroma_probe.calls}) == 2
    statuses = {item.criterion_id: item.status for item in ledger.criteria}
    assert statuses["control.provider-probe-reconciled"] == "pass"
    assert statuses["control.chroma-probe-reconciled"] == "pass"
    assert statuses["audit.probe-events-instrumented"] == "pass"
    assert "llm.openai" in (store.root / "events.ndjson").read_text()
    store.scan_recursive()

    with pytest.raises(RuntimeError, match="already completed"):
        runtime.execute_authorized_probes(provider_probe=provider, chroma_probe=chroma_probe)
    assert len(provider.calls) == len(chroma_probe.calls) == 1


def test_probe_result_mismatch_consumes_authorization_and_fails_closed(tmp_path: Path) -> None:
    runtime, store, _ledger, _campaign_config, _database = _runtime(tmp_path)
    audit, chroma = _local_inspectors(store)
    runtime.execute_local_gates(
        audit_inspector=audit,
        chroma_inspector=chroma,
        chroma_seeder=_Seeder(),
    )

    class BadProbe(_Probe):
        def execute_paid_probe(self, authorization: PaidProbeAuthorization) -> PaidProbeResult:
            return replace(super().execute_paid_probe(authorization), operation_id="wrong-operation")

    with pytest.raises(ValueError, match="match its authorization"):
        runtime.execute_authorized_probes(
            provider_probe=BadProbe("provider"),
            chroma_probe=_Probe("chroma"),
        )
    events = store.read_events()
    assert len([item for item in events if item["type"] == "control.probe.authorized"]) == 1
    assert not [item for item in events if item["type"] == "control.probe.reconciled"]


def test_local_gate_refuses_inspector_or_seeder_misrepresentation(tmp_path: Path) -> None:
    runtime, store, _ledger, _campaign_config, _database = _runtime(tmp_path)
    audit, chroma = _local_inspectors(store)

    class DroppingSeeder(_Seeder):
        def seed_exactly(
            self, tenant_id: str, documents: tuple[SyntheticChromaDocument, ...]
        ) -> tuple[ChromaCorpusDocument, ...]:
            return super().seed_exactly(tenant_id, documents[:-1])

    with pytest.raises(ValueError, match="confirm exactly three"):
        runtime.execute_local_gates(
            audit_inspector=audit,
            chroma_inspector=chroma,
            chroma_seeder=DroppingSeeder(),
        )
