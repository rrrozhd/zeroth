from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

import pytest

from release.live_evaluation.config import CampaignConfig
from release.live_evaluation.control_gate import (
    BudgetGateProof,
    ChromaCorpusDocument,
    ChromaIdentity,
    ControlPlaneGate,
    PaidProbeResult,
    ProbeAlreadyAuthorizedError,
    SignedAuditReadiness,
)
from release.live_evaluation.criteria import original_acceptance_criteria
from release.live_evaluation.evidence import EvidenceStore
from release.live_evaluation.ledger import CampaignLedger


def _campaign(tmp_path: Path) -> CampaignConfig:
    return CampaignConfig.model_validate(
        {
            "schema_version": 1,
            "campaign_id": "evaluation-control-gate",
            "tenant_id": "evaluation-control-gate",
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


def _gate(tmp_path: Path) -> tuple[ControlPlaneGate, EvidenceStore, CampaignLedger]:
    store = EvidenceStore(tmp_path / "evidence")
    ledger = CampaignLedger(store, original_acceptance_criteria())
    return ControlPlaneGate(store=store, ledger=ledger, campaign=_campaign(tmp_path)), store, ledger


def _source_event(store: EvidenceStore, name: str) -> str:
    event_id = store.append_event("control.local-proof", {"name": name})
    return f"events.ndjson#{event_id}"


def _prepare_local_gates(gate: ControlPlaneGate, store: EvidenceStore) -> None:
    gate.record_budget_gate(
        BudgetGateProof(
            tenant_limit_usd=Decimal("10.00"),
            run_limit_usd=Decimal("0.25"),
            concurrent_admission_proved=True,
            over_limit_rejected=True,
            commit_and_release_proved=True,
            restart_recovery_proved=True,
            evidence_references=(_source_event(store, "budget-proof"),),
        )
    )
    gate.record_signed_audit_readiness(
        SignedAuditReadiness(
            state="signed",
            algorithm="hmac-sha256",
            signing_reference="evaluation.control.signing",
            evidence_reference=_source_event(store, "signing-proof"),
        )
    )
    gate.record_chroma_readiness(
        ChromaIdentity(
            "chromadb/chroma:1.5.6",
            "127.0.0.1",
            8121,
            "zeroth-evaluation-chroma",
            "1.5.6",
            _source_event(store, "chroma-proof"),
        ),
        tuple(
            ChromaCorpusDocument(
                document_id=f"document-{index}",
                tenant_id="evaluation-control-gate",
                sha256=f"sha256:{index:064x}",
            )
            for index in range(1, 4)
        ),
    )


def test_local_gate_records_exact_caps_signed_readiness_and_three_hashes(
    tmp_path: Path,
) -> None:
    gate, store, ledger = _gate(tmp_path)
    budget_refs = tuple(
        _source_event(store, name) for name in ("concurrency", "reject", "commit", "recovery")
    )
    gate.record_budget_gate(
        BudgetGateProof(
            tenant_limit_usd=Decimal("10.00"),
            run_limit_usd=Decimal("0.25"),
            concurrent_admission_proved=True,
            over_limit_rejected=True,
            commit_and_release_proved=True,
            restart_recovery_proved=True,
            evidence_references=budget_refs,
        )
    )
    gate.record_signed_audit_readiness(
        SignedAuditReadiness(
            state="signed",
            algorithm="hmac-sha256",
            signing_reference="evaluation.control.signing",
            evidence_reference=_source_event(store, "signed-chain"),
        )
    )
    documents = tuple(
        ChromaCorpusDocument(
            document_id=f"synthetic-{index}",
            tenant_id="evaluation-control-gate",
            sha256=f"sha256:{index:064x}",
        )
        for index in range(1, 4)
    )
    gate.record_chroma_readiness(
        ChromaIdentity(
            image="chromadb/chroma:1.5.6",
            host="127.0.0.1",
            port=8121,
            instance_id="zeroth-evaluation-chroma",
            api_version="1.5.6",
            evidence_reference=_source_event(store, "chroma-health"),
        ),
        documents,
    )

    statuses = {item.criterion_id: item.status for item in ledger.criteria}
    for criterion_id in (
        "control.tenant-budget-10",
        "control.run-budget-025",
        "control.budget-concurrency",
        "control.budget-rejection",
        "control.budget-commit-release",
        "control.budget-recovery",
        "control.audit-signed",
        "control.chroma-pinned-loopback",
        "control.chroma-corpus-seeded",
    ):
        assert statuses[criterion_id] == "pass"
    serialized = (store.root / "events.ndjson").read_text()
    assert "synthetic corpus content" not in serialized
    assert "evaluation.control.signing" in serialized
    assert len(documents) == 3


@pytest.mark.parametrize(
    ("tenant_limit", "run_limit"),
    [("9.99", "0.25"), ("10.00", "0.24"), ("10.01", "0.25")],
)
def test_budget_gate_requires_exact_campaign_limits(
    tmp_path: Path, tenant_limit: str, run_limit: str
) -> None:
    gate, store, _ = _gate(tmp_path)
    references = (_source_event(store, "proof"),)
    with pytest.raises(ValueError, match="exactly"):
        gate.record_budget_gate(
            BudgetGateProof(
                tenant_limit_usd=Decimal(tenant_limit),
                run_limit_usd=Decimal(run_limit),
                concurrent_admission_proved=True,
                over_limit_rejected=True,
                commit_and_release_proved=True,
                restart_recovery_proved=True,
                evidence_references=references,
            )
        )


def test_chroma_gate_rejects_nonloopback_latest_or_wrong_corpus(tmp_path: Path) -> None:
    gate, store, _ = _gate(tmp_path)
    evidence = _source_event(store, "health")
    with pytest.raises(ValueError, match="loopback"):
        gate.record_chroma_readiness(
            ChromaIdentity("chromadb/chroma:1.5.6", "0.0.0.0", 8121, "c", "1.5.6", evidence),
            (),
        )
    with pytest.raises(ValueError, match="pinned"):
        ChromaIdentity("chromadb/chroma:latest", "127.0.0.1", 8121, "c", "1.5.6", evidence)


def test_probe_authorization_is_one_shot_across_resume_and_reconciles_identities(
    tmp_path: Path,
) -> None:
    gate, store, ledger = _gate(tmp_path)
    _prepare_local_gates(gate, store)
    authorization = gate.authorize_paid_probe(
        kind="provider",
        operation_id="op-provider-1",
        run_id="run-provider-1",
    )
    assert authorization.credential_reference == "llm.openai"
    assert not hasattr(authorization, "provider_key")

    resumed = ControlPlaneGate(store=store, ledger=ledger, campaign=_campaign(tmp_path))
    with pytest.raises(ProbeAlreadyAuthorizedError, match="provider"):
        resumed.authorize_paid_probe(
            kind="provider", operation_id="op-provider-2", run_id="run-provider-2"
        )

    reference = resumed.reconcile_paid_probe(
        PaidProbeResult(
            kind="provider",
            operation_id="op-provider-1",
            run_id="run-provider-1",
            audit_event_id="audit-provider-1",
            cost_event_id="cost-provider-1",
            provider_request_id="request-provider-1",
            connector_request_id=None,
            request_count=1,
            cache_hit=False,
            audit_chain_signed=True,
            cleanup_state="committed",
            measured_cost_usd=Decimal("0.000123"),
        )
    )
    assert reference.startswith("events.ndjson#")
    statuses = {item.criterion_id: item.status for item in ledger.criteria}
    assert statuses["control.provider-credential-valid"] == "pass"
    assert statuses["control.provider-credential-not-persisted"] == "pass"
    assert statuses["control.provider-probe-reconciled"] == "pass"

    with pytest.raises(ValueError, match="already reconciled"):
        resumed.reconcile_paid_probe(
            PaidProbeResult(
                kind="provider",
                operation_id="op-provider-1",
                run_id="run-provider-1",
                audit_event_id="audit-provider-2",
                cost_event_id="cost-provider-2",
                provider_request_id="request-provider-2",
                connector_request_id=None,
                request_count=1,
                cache_hit=False,
                audit_chain_signed=True,
                cleanup_state="committed",
                measured_cost_usd=Decimal("0.000123"),
            )
        )


def test_provider_probe_reconciles_when_upstream_request_id_was_unavailable(
    tmp_path: Path,
) -> None:
    gate, store, ledger = _gate(tmp_path)
    _prepare_local_gates(gate, store)
    gate.authorize_paid_probe(
        kind="provider",
        operation_id="op-provider-without-upstream-id",
        run_id="run-provider-without-upstream-id",
    )

    reference = gate.reconcile_paid_probe(
        PaidProbeResult(
            kind="provider",
            operation_id="op-provider-without-upstream-id",
            run_id="run-provider-without-upstream-id",
            audit_event_id="audit-provider-without-upstream-id",
            cost_event_id="cost-provider-without-upstream-id",
            provider_request_id=None,
            connector_request_id=None,
            request_count=1,
            cache_hit=False,
            audit_chain_signed=True,
            cleanup_state="committed",
            measured_cost_usd=Decimal("0.000123"),
        )
    )

    assert reference.startswith("events.ndjson#")
    reconciled = [
        event for event in store.read_events() if event["type"] == "control.probe.reconciled"
    ]
    assert reconciled[0]["correlation"].get("provider_request_id") is None
    statuses = {item.criterion_id: item.status for item in ledger.criteria}
    assert statuses["control.provider-probe-reconciled"] == "pass"


def test_chroma_probe_still_requires_upstream_request_identity(tmp_path: Path) -> None:
    gate, store, _ = _gate(tmp_path)
    _prepare_local_gates(gate, store)
    gate.authorize_paid_probe(
        kind="chroma",
        operation_id="op-chroma-without-upstream-id",
        run_id="run-chroma-without-upstream-id",
    )

    with pytest.raises(ValueError, match="Chroma probe requires"):
        gate.reconcile_paid_probe(
            PaidProbeResult(
                kind="chroma",
                operation_id="op-chroma-without-upstream-id",
                run_id="run-chroma-without-upstream-id",
                audit_event_id="audit-chroma-without-upstream-id",
                cost_event_id="cost-chroma-without-upstream-id",
                provider_request_id=None,
                connector_request_id=None,
                request_count=1,
                cache_hit=False,
                audit_chain_signed=True,
                cleanup_state="committed",
                measured_cost_usd=Decimal("0.000123"),
            )
        )


def test_probe_authorization_has_one_concurrent_winner(tmp_path: Path) -> None:
    gate, store, ledger = _gate(tmp_path)
    _prepare_local_gates(gate, store)

    def attempt(index: int) -> str:
        candidate = ControlPlaneGate(
            store=store,
            ledger=ledger,
            campaign=_campaign(tmp_path),
        )
        try:
            candidate.authorize_paid_probe(
                kind="provider",
                operation_id=f"op-concurrent-{index}",
                run_id=f"run-concurrent-{index}",
            )
        except ProbeAlreadyAuthorizedError:
            return "rejected"
        return "authorized"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(pool.map(attempt, (1, 2)))

    assert sorted(outcomes) == ["authorized", "rejected"]
    events = [
        event for event in store.read_events() if event.get("type") == "control.probe.authorized"
    ]
    assert len(events) == 1


def test_chroma_probe_requires_connector_identity_and_exactly_one_call(tmp_path: Path) -> None:
    gate, store, _ = _gate(tmp_path)
    _prepare_local_gates(gate, store)
    gate.authorize_paid_probe(kind="chroma", operation_id="op-c", run_id="run-c")
    with pytest.raises(ValueError, match="connector request"):
        gate.reconcile_paid_probe(
            PaidProbeResult(
                kind="chroma",
                operation_id="op-c",
                run_id="run-c",
                audit_event_id="audit-c",
                cost_event_id="cost-c",
                provider_request_id="request-c",
                connector_request_id=None,
                request_count=1,
                cache_hit=False,
                audit_chain_signed=True,
                cleanup_state="committed",
                measured_cost_usd=Decimal("0.00001"),
            )
        )


def test_both_probe_results_open_instrumentation_gate(tmp_path: Path) -> None:
    gate, store, ledger = _gate(tmp_path)
    _prepare_local_gates(gate, store)
    for kind in ("provider", "chroma"):
        gate.authorize_paid_probe(
            kind=kind,
            operation_id=f"op-{kind}",
            run_id=f"run-{kind}",
        )
        gate.reconcile_paid_probe(
            PaidProbeResult(
                kind=kind,
                operation_id=f"op-{kind}",
                run_id=f"run-{kind}",
                audit_event_id=f"audit-{kind}",
                cost_event_id=f"cost-{kind}",
                provider_request_id=f"request-{kind}",
                connector_request_id=("connector-chroma" if kind == "chroma" else None),
                request_count=1,
                cache_hit=False,
                audit_chain_signed=True,
                cleanup_state="committed",
                measured_cost_usd=Decimal("0.00001"),
            )
        )

    statuses = {item.criterion_id: item.status for item in ledger.criteria}
    assert statuses["control.provider-probe-reconciled"] == "pass"
    assert statuses["control.chroma-probe-reconciled"] == "pass"
    assert statuses["audit.probe-events-instrumented"] == "pass"


def test_probe_gate_accepts_only_logical_references_and_fails_closed(tmp_path: Path) -> None:
    gate, store, _ = _gate(tmp_path)
    with pytest.raises(ValueError):
        SignedAuditReadiness(
            state="signed",
            algorithm="hmac-sha256",
            signing_reference="sk-proj-this-is-not-a-logical-reference",
            evidence_reference="events.ndjson#missing",
        )
    _prepare_local_gates(gate, store)
    gate.authorize_paid_probe(kind="provider", operation_id="op", run_id="run")
    with pytest.raises(ValueError, match="exactly one"):
        gate.reconcile_paid_probe(
            PaidProbeResult(
                kind="provider",
                operation_id="op",
                run_id="run",
                audit_event_id="audit",
                cost_event_id="cost",
                provider_request_id="request",
                connector_request_id=None,
                request_count=2,
                cache_hit=False,
                audit_chain_signed=True,
                cleanup_state="committed",
                measured_cost_usd=Decimal("0.00001"),
            )
        )
