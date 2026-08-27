from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path

import pytest

from release.live_evaluation.batch_provider_economics import (
    BatchEconomicsObservation,
    BatchProviderEconomicsHarness,
    LiveBatchGate,
    ParentBatchObservation,
    ReadinessAttestation,
    main,
    verify_sealed_bundle,
)
from release.live_evaluation.campaign_http import provider_acknowledgement
from release.live_evaluation.config import CampaignConfig


def _campaign(tmp_path: Path) -> CampaignConfig:
    return CampaignConfig.model_validate(
        {
            "schema_version": 1,
            "campaign_id": "evaluation-batch-economics",
            "tenant_id": "evaluation-batch-economics",
            "provider": "openai",
            "model": "openai/gpt-4o-mini",
            "embedding_model": "openai/text-embedding-3-small",
            "vector_backend": "chroma",
            "campaign_budget_usd": "10.00",
            "per_run_cap_usd": "0.25",
            "provider_secret_ref": "llm.openai",
            "artifact_root": str(tmp_path / "artifacts"),
            "action_sink_root": str(tmp_path / "artifacts/action-sink"),
        }
    )


def _gate(tmp_path: Path, *, installed: bool = True) -> LiveBatchGate:
    campaign = _campaign(tmp_path)
    return LiveBatchGate(
        campaign=campaign,
        provider_execution_enabled=True,
        external_cost_acknowledgement=provider_acknowledgement(campaign.campaign_id),
        readiness=ReadinessAttestation(
            campaign_id=campaign.campaign_id,
            tenant_id=campaign.tenant_id,
            logical_secret_ref=campaign.provider_secret_ref,
            installed=installed,
            provider_probe_reconciled=installed,
            provider_request_id="provider-readiness-probe",
            operation_id="operation-readiness-probe",
            run_id="run-readiness-probe",
            audit_event_id="audit-readiness-probe",
            cost_event_id="cost-readiness-probe",
            measured_cost_usd=Decimal("0.000001"),
            campaign_spend_before_usd=Decimal("0.000001"),
            audit_signed=True,
        ),
    )


def test_readiness_accepts_unavailable_upstream_request_id(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    gate = replace(
        gate,
        readiness=replace(gate.readiness, provider_request_id=None),
    )

    gate.validate()


def _child(repetition: int, item_index: int, parent_run_id: str) -> BatchEconomicsObservation:
    amount = Decimal("0.000020")
    return BatchEconomicsObservation(
        campaign_id="evaluation-batch-economics",
        repetition=repetition,
        item_index=item_index,
        parent_run_id=parent_run_id,
        child_run_id=f"service-child-{repetition}-{item_index}",
        operation_id=f"service-operation-{repetition}-{item_index}",
        provider_request_id=f"provider-request-{repetition}-{item_index}",
        audit_event_id=f"audit-event-{repetition}-{item_index}",
        cost_event_ids=(f"cost-event-{repetition}-{item_index}",),
        reservation_id=f"service-reservation-{repetition}-{item_index}",
        reservation_operation_id=f"service-operation-{repetition}-{item_index}",
        reservation_status="committed",
        reserved_max_cost_usd=Decimal("0.000025"),
        reservation_actual_cost_usd=amount,
        reservation_released_cost_usd=Decimal("0.000005"),
        reservation_cleanup_status="complete",
        cache_hit=False,
        audit_cost_usd=amount,
        run_cost_usd=amount,
        local_cost_usd=amount,
        economics_cost_usd=amount,
        audit_signed=True,
        audit_chain_verified=True,
        parent_child_linked=True,
    )


def _parent(repetition: int) -> ParentBatchObservation:
    parent_run_id = f"service-parent-{repetition}"
    actual = Decimal("0.000160")
    return ParentBatchObservation(
        campaign_id="evaluation-batch-economics",
        repetition=repetition,
        parent_run_id=parent_run_id,
        status="succeeded",
        configured_concurrency=4,
        observed_peak_concurrency=4,
        campaign_spend_after_usd=Decimal("0.000001") + actual * repetition,
        audit_cost_usd=actual,
        run_cost_usd=actual,
        local_cost_usd=actual,
        economics_cost_usd=actual,
        audit_signed=True,
        audit_chain_verified=True,
        children=tuple(_child(repetition, index, parent_run_id) for index in range(8)),
    )


class _Adapter:
    def __init__(self, mutate=lambda value: value) -> None:
        self.submissions = []
        self.mutate = mutate

    async def submit_parent(self, submission):
        self.submissions.append(submission)
        return self.mutate(_parent(submission.repetition))


def test_dry_run_plans_three_parent_submissions_without_minting_service_ids(tmp_path: Path) -> None:
    plan = BatchProviderEconomicsHarness(_gate(tmp_path)).dry_run()
    assert plan.criterion_id == "batching.provider-economics"
    assert (plan.repetitions, plan.items_per_repetition, plan.concurrency) == (3, 8, 4)
    assert [submission.repetition for submission in plan.submissions] == [1, 2, 3]
    assert all(
        submission.items == 8 and submission.concurrency == 4 for submission in plan.submissions
    )
    assert all(
        submission.per_run_cap_usd == Decimal("0.25")
        and submission.campaign_cap_usd == Decimal("10.00")
        for submission in plan.submissions
    )
    encoded = json.dumps(plan.as_dict(), sort_keys=True)
    for service_owned_field in ("parent_run_id", "child_run_id", "operation_id"):
        assert service_owned_field not in encoded
    assert plan.per_run_cap_usd == Decimal("0.25")
    assert plan.campaign_cap_usd == Decimal("10.00")


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda gate: replace(gate, provider_execution_enabled=False), "disabled"),
        (lambda gate: replace(gate, external_cost_acknowledgement="wrong"), "acknowledgement"),
        (
            lambda gate: replace(gate, readiness=replace(gate.readiness, installed=False)),
            "not installed",
        ),
        (
            lambda gate: replace(
                gate, readiness=replace(gate.readiness, provider_probe_reconciled=False)
            ),
            "probe",
        ),
        (
            lambda gate: replace(
                gate, readiness=replace(gate.readiness, logical_secret_ref="llm.other")
            ),
            "readiness identity",
        ),
    ],
)
def test_live_gate_fails_closed_without_exact_nonsecret_readiness(
    tmp_path: Path, mutation, message: str
) -> None:
    with pytest.raises(RuntimeError, match=message):
        BatchProviderEconomicsHarness(mutation(_gate(tmp_path))).dry_run()


def test_readiness_and_plan_never_serialize_secret_values(tmp_path: Path) -> None:
    encoded = json.dumps(
        BatchProviderEconomicsHarness(_gate(tmp_path)).dry_run().as_dict(), sort_keys=True
    )
    for forbidden in ("provider_secret_ref", "logical_secret_ref", "api_key", "llm.openai"):
        assert forbidden not in encoded


def test_cli_dry_run_accepts_nonsecret_probe_attestation(tmp_path: Path, capsys) -> None:
    campaign = _campaign(tmp_path)
    campaign_path = tmp_path / "campaign.json"
    campaign_path.write_text(campaign.model_dump_json())
    readiness_path = tmp_path / "readiness.json"
    readiness = _gate(tmp_path).readiness
    payload = asdict(readiness)
    payload["measured_cost_usd"] = format(readiness.measured_cost_usd, "f")
    payload["campaign_spend_before_usd"] = format(readiness.campaign_spend_before_usd, "f")
    readiness_path.write_text(json.dumps(payload))
    assert (
        main(
            [
                "dry-run",
                "--campaign-config",
                str(campaign_path),
                "--readiness-attestation",
                str(readiness_path),
                "--enable-provider-execution",
                "--acknowledge-external-cost",
                provider_acknowledgement(campaign.campaign_id),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert '"criterion_id": "batching.provider-economics"' in output
    assert "llm.openai" not in output


def test_execute_submits_exactly_three_real_parents_and_collects_24_service_children(
    tmp_path: Path,
) -> None:
    adapter = _Adapter()
    result = asyncio.run(BatchProviderEconomicsHarness(_gate(tmp_path)).execute(adapter))
    assert [item.repetition for item in adapter.submissions] == [1, 2, 3]
    assert len(result.parent_observations) == 3
    assert sum(len(parent.children) for parent in result.parent_observations) == 24
    assert result.total_cost_usd == Decimal("0.000480")
    assert result.campaign_total_cost_usd == Decimal("0.000481")
    assert result.passed


def test_each_child_exposes_operation_keyed_atomic_reservation_evidence() -> None:
    child = _child(1, 0, "service-parent-1")

    assert child.reservation_operation_id == child.operation_id
    assert child.reservation_status == "committed"
    assert child.reservation_cleanup_status == "complete"
    assert child.reservation_actual_cost_usd == child.audit_cost_usd


def test_execute_fails_closed_on_invalid_adapter_observation(tmp_path: Path) -> None:
    class Adapter:
        def __init__(self) -> None:
            self.calls = 0

        async def submit_parent(self, submission):
            self.calls += 1
            return object()

    adapter = Adapter()
    with pytest.raises(TypeError, match="parent observation"):
        asyncio.run(BatchProviderEconomicsHarness(_gate(tmp_path)).execute(adapter))
    assert adapter.calls == 1


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda parent: replace(parent, status="failed"), "did not succeed"),
        (lambda parent: replace(parent, observed_peak_concurrency=3), "exact concurrency four"),
        (lambda parent: replace(parent, children=parent.children[:-1]), "exactly eight children"),
        (
            lambda parent: replace(
                parent, children=(replace(parent.children[0], item_index=1),) + parent.children[1:]
            ),
            "item indexes",
        ),
        (lambda parent: replace(parent, audit_cost_usd=Decimal("0.000170")), "parent plane totals"),
    ],
)
def test_parent_contract_rejects_incomplete_execution_evidence(
    tmp_path: Path, mutation, message: str
) -> None:
    def mutate(parent):
        return mutation(parent) if parent.repetition == 1 else parent

    with pytest.raises(RuntimeError, match=message):
        asyncio.run(BatchProviderEconomicsHarness(_gate(tmp_path)).execute(_Adapter(mutate)))


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda child: replace(child, parent_run_id="wrong-parent"), "identity reconciliation"),
        (
            lambda child: replace(child, cost_event_ids=("cost-a", "cost-b")),
            "exactly one cost event",
        ),
        (lambda child: replace(child, economics_cost_usd=Decimal("0.000022")), "child totals"),
        (
            lambda child: replace(child, reservation_operation_id="wrong-operation"),
            "reservation identity",
        ),
        (
            lambda child: replace(child, reservation_status="held_ambiguous"),
            "reservation was not committed",
        ),
        (
            lambda child: replace(child, reservation_actual_cost_usd=Decimal("0.000018")),
            "reservation actual cost",
        ),
        (
            lambda child: replace(child, reservation_cleanup_status="pending"),
            "cleanup",
        ),
        (
            lambda child: replace(child, reservation_released_cost_usd=Decimal("0")),
            "remainder",
        ),
    ],
)
def test_child_contract_rejects_identity_and_economics_drift(
    tmp_path: Path, mutation, message: str
) -> None:
    def mutate_parent(parent):
        if parent.repetition != 1:
            return parent
        return replace(parent, children=(mutation(parent.children[0]),) + parent.children[1:])

    with pytest.raises(RuntimeError, match=message):
        asyncio.run(BatchProviderEconomicsHarness(_gate(tmp_path)).execute(_Adapter(mutate_parent)))


def test_non_cache_child_accepts_truthful_unavailable_provider_request_id(
    tmp_path: Path,
) -> None:
    def mutate(parent):
        return replace(
            parent,
            children=(replace(parent.children[0], provider_request_id=None),) + parent.children[1:],
        )

    result = asyncio.run(BatchProviderEconomicsHarness(_gate(tmp_path)).execute(_Adapter(mutate)))
    assert result.passed is True
    assert result.parent_observations[0].children[0].provider_request_id is None


def test_contract_rejects_duplicate_service_minted_identities(tmp_path: Path) -> None:
    def mutate(parent):
        if parent.repetition != 2:
            return parent
        duplicate = replace(
            parent.children[0],
            child_run_id="service-child-1-0",
            operation_id="service-operation-1-0",
            reservation_id="service-reservation-1-0",
            reservation_operation_id="service-operation-1-0",
            provider_request_id="provider-request-1-0",
            audit_event_id="audit-event-1-0",
            cost_event_ids=("cost-event-1-0",),
        )
        return replace(parent, children=(duplicate,) + parent.children[1:])

    with pytest.raises(RuntimeError, match="duplicate"):
        asyncio.run(BatchProviderEconomicsHarness(_gate(tmp_path)).execute(_Adapter(mutate)))


def test_contract_rejects_campaign_drift(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="progression"):
        asyncio.run(
            BatchProviderEconomicsHarness(_gate(tmp_path)).execute(
                _Adapter(
                    lambda parent: (
                        replace(parent, campaign_spend_after_usd=Decimal("0.5"))
                        if parent.repetition == 1
                        else parent
                    )
                )
            )
        )


def test_gate_requires_exact_approved_caps(tmp_path: Path) -> None:
    campaign = _campaign(tmp_path).model_copy(update={"per_run_cap_usd": Decimal("0.20")})
    with pytest.raises(RuntimeError, match=r"exactly \$0.25"):
        BatchProviderEconomicsHarness(replace(_gate(tmp_path), campaign=campaign)).dry_run()
    campaign = _campaign(tmp_path).model_copy(update={"campaign_budget_usd": Decimal("9.00")})
    with pytest.raises(RuntimeError, match=r"exactly \$10.00"):
        BatchProviderEconomicsHarness(replace(_gate(tmp_path), campaign=campaign)).dry_run()


def test_gate_requires_worst_case_campaign_headroom_before_any_submission(
    tmp_path: Path,
) -> None:
    gate = _gate(tmp_path)
    gate = replace(
        gate,
        readiness=replace(
            gate.readiness,
            campaign_spend_before_usd=Decimal("9.30"),
        ),
    )

    with pytest.raises(RuntimeError, match="headroom"):
        BatchProviderEconomicsHarness(gate).dry_run()


def test_observations_reject_truthy_non_boolean_evidence(tmp_path: Path) -> None:
    del tmp_path
    with pytest.raises(ValueError, match="boolean"):
        replace(_child(1, 0, "service-parent-1"), audit_signed="yes")
    with pytest.raises(ValueError, match="boolean"):
        replace(_parent(1), audit_chain_verified=1)


def test_sealer_emits_only_exact_criterion_and_verifiable_checksums(tmp_path: Path) -> None:
    harness = BatchProviderEconomicsHarness(_gate(tmp_path))
    destination = tmp_path / "sealed"
    harness.seal(asyncio.run(harness.execute(_Adapter())), destination)
    verification = verify_sealed_bundle(destination)
    assert verification["verified"] is True
    assert verification["criterion_id"] == "batching.provider-economics"
    acceptance = json.loads((destination / "acceptance.json").read_text())
    assert acceptance["criteria"] == [
        {
            "criterion_id": "batching.provider-economics",
            "evidence": ["reconciliation/batch-provider-economics.json"],
            "note": None,
            "status": "pass",
        }
    ]
    manifest = json.loads((destination / "manifest.json").read_text())
    assert manifest["parent_runs_planned"] == 3
    assert manifest["child_observations_required"] == 24


def test_checksum_verifier_rejects_tampering(tmp_path: Path) -> None:
    harness = BatchProviderEconomicsHarness(_gate(tmp_path))
    destination = harness.seal(asyncio.run(harness.execute(_Adapter())), tmp_path / "sealed")
    reconciliation = destination / "reconciliation/batch-provider-economics.json"
    reconciliation.write_text(reconciliation.read_text() + " ")
    with pytest.raises(RuntimeError, match="checksum"):
        verify_sealed_bundle(destination)


def test_sealer_rejects_secret_shaped_provider_identity(tmp_path: Path) -> None:
    def mutate(parent):
        if parent.repetition != 1:
            return parent
        child = replace(
            parent.children[0], provider_request_id="sk-proj-abcdefghijklmnopqrstuvwxyz123456"
        )
        return replace(parent, children=(child,) + parent.children[1:])

    harness = BatchProviderEconomicsHarness(_gate(tmp_path))
    result = asyncio.run(harness.execute(_Adapter(mutate)))
    with pytest.raises(ValueError, match="secret-shaped"):
        harness.seal(result, tmp_path / "sealed")


def test_sealer_rejects_forged_plan(tmp_path: Path) -> None:
    harness = BatchProviderEconomicsHarness(_gate(tmp_path))
    result = asyncio.run(harness.execute(_Adapter()))
    forged = replace(result, plan=replace(result.plan, per_run_cap_usd=Decimal("9.00")))
    with pytest.raises(RuntimeError, match="plan does not match"):
        harness.seal(forged, tmp_path / "sealed")


def test_checksum_manifest_covers_every_sealed_file_except_itself(tmp_path: Path) -> None:
    harness = BatchProviderEconomicsHarness(_gate(tmp_path))
    destination = harness.seal(asyncio.run(harness.execute(_Adapter())), tmp_path / "sealed")
    rows = (destination / "SHA256SUMS").read_text().splitlines()
    covered = {line.split("  ", 1)[1] for line in rows}
    expected = {
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file() and path.name != "SHA256SUMS"
    }
    assert covered == expected
    for line in rows:
        digest, relative = line.split("  ", 1)
        assert hashlib.sha256((destination / relative).read_bytes()).hexdigest() == digest
