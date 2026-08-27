from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import release.live_evaluation.accelerated_v3_w2_driver as subject
from release.live_evaluation.batch_provider_economics import (
    BatchEconomicsObservation,
    ParentBatchObservation,
)


PROFILE = Path("release/live_evaluation/accelerated-acceptance-v3.json")


def _sealed_bundle(root: Path, files: dict[str, object] | None = None) -> Path:
    root.mkdir()
    values = files or {"manifest.json": {"status": "verified"}}
    rows: list[str] = []
    for name, value in sorted(values.items()):
        payload = (json.dumps(value, sort_keys=True) + "\n").encode()
        (root / name).write_bytes(payload)
        rows.append(f"{hashlib.sha256(payload).hexdigest()}  {name}")
    (root / "SHA256SUMS").write_text("\n".join(rows) + "\n")
    return root


def _w1(root: Path) -> Path:
    return _sealed_bundle(
        root,
        {
            "manifest.json": {
                "checkpoint": "post-stop-cost-rollup-remediation",
                "campaign_id": "evaluation-studio-v1",
                "provider_calls_performed": 0,
                "run_submissions_performed": 0,
            }
        },
    )


def _w2_prior(root: Path) -> Path:
    return _sealed_bundle(
        root,
        {
            "manifest.json": {
                "campaign_id": "evaluation-studio-v1",
                "historical_provider_calls_reconciled": 16,
                "parents": [
                    {"repetition": 1, "status": "succeeded"},
                    {"repetition": 2, "status": "succeeded"},
                ],
            },
            "acceptance.json": {
                "criteria": [
                    {"criterion_id": "workflow2.happy-1", "status": "pass"},
                    {"criterion_id": "workflow2.happy-2", "status": "pass"},
                ]
            },
        },
    )


def test_load_profile_requires_exact_unarmed_v3_contract() -> None:
    profile = subject.load_profile(PROFILE)

    assert profile["profile_id"] == "evaluation-studio-v1-accelerated-demo-v3"
    assert profile["status"] == "proposed_unarmed"
    assert profile["authorization_phrase"] == subject.AUTHORIZATION_PHRASE
    assert profile["budgets"]["maximum_new_provider_calls"] == 12
    assert profile["budgets"]["maximum_new_live_runs"] == 2


def test_preflight_rejects_wrong_authorization_before_precondition_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    touched = False

    def forbidden(_root: Path) -> str:
        nonlocal touched
        touched = True
        pytest.fail("authorization must gate filesystem preconditions")

    monkeypatch.setattr(subject, "verify_sealed_bundle", forbidden)
    with pytest.raises(subject.AcceleratedV3BlockedError, match="v3_authorization_invalid"):
        subject.preflight(
            profile_path=PROFILE,
            authorization="not-authorized",
            remediation_bundle=tmp_path / "missing",
            prior_w2_bundle=tmp_path / "missing-w2",
        )
    assert touched is False


def test_preflight_verifies_checksum_sealed_w1_remediation(tmp_path: Path) -> None:
    sealed = _w1(tmp_path / "sealed")
    prior_w2 = _w2_prior(tmp_path / "prior-w2")
    result = subject.preflight(
        profile_path=PROFILE,
        authorization=subject.AUTHORIZATION_PHRASE,
        remediation_bundle=sealed,
        prior_w2_bundle=prior_w2,
    )

    assert result.profile_sha256 == hashlib.sha256(PROFILE.read_bytes()).hexdigest()
    assert result.remediation_checksum_sha256 == hashlib.sha256(
        (sealed / "SHA256SUMS").read_bytes()
    ).hexdigest()

    (sealed / "manifest.json").write_text(json.dumps({"status": "changed"}))
    with pytest.raises(subject.AcceleratedV3BlockedError, match="remediation_bundle_invalid"):
        subject.preflight(
            profile_path=PROFILE,
            authorization=subject.AUTHORIZATION_PHRASE,
            remediation_bundle=sealed,
            prior_w2_bundle=prior_w2,
        )


def test_preflight_rejects_unproven_prior_w2_repetitions(tmp_path: Path) -> None:
    w1 = _w1(tmp_path / "w1")
    incomplete = _sealed_bundle(
        tmp_path / "w2",
        {
            "manifest.json": {
                "campaign_id": "evaluation-studio-v1",
                "historical_provider_calls_reconciled": 8,
                "parents": [{"repetition": 1, "status": "succeeded"}],
            },
            "acceptance.json": {
                "criteria": [
                    {"criterion_id": "workflow2.happy-1", "status": "pass"},
                    {"criterion_id": "workflow2.happy-2", "status": "not_run"},
                ]
            },
        },
    )
    with pytest.raises(subject.AcceleratedV3BlockedError, match="prior_w2_bundle_invalid"):
        subject.preflight(
            profile_path=PROFILE,
            authorization=subject.AUTHORIZATION_PHRASE,
            remediation_bundle=w1,
            prior_w2_bundle=incomplete,
        )


def test_single_parent_plan_is_exactly_repetition_three() -> None:
    plan = subject.single_parent_plan(
        campaign_id="evaluation-studio-v1",
        campaign_spend_before_usd="0.00406632",
    )

    assert plan.repetitions == 1
    assert len(plan.submissions) == 1
    assert plan.submissions[0].repetition == 3
    assert plan.submissions[0].items == 8
    assert plan.submissions[0].concurrency == 4


def _parent(spend_before: Decimal) -> ParentBatchObservation:
    amount = Decimal("0.000020")
    children = tuple(
        BatchEconomicsObservation(
            campaign_id="evaluation-studio-v1",
            repetition=3,
            item_index=index,
            parent_run_id="parent-3",
            child_run_id=f"child-3-{index}",
            operation_id=f"operation-3-{index}",
            provider_request_id=f"provider-3-{index}",
            audit_event_id=f"audit-3-{index}",
            cost_event_ids=(f"cost-3-{index}",),
            reservation_id=f"reservation-3-{index}",
            reservation_operation_id=f"operation-3-{index}",
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
        for index in range(8)
    )
    total = amount * 8
    return ParentBatchObservation(
        campaign_id="evaluation-studio-v1",
        repetition=3,
        parent_run_id="parent-3",
        status="succeeded",
        configured_concurrency=4,
        observed_peak_concurrency=4,
        campaign_spend_after_usd=spend_before + total,
        audit_cost_usd=total,
        run_cost_usd=total,
        local_cost_usd=total,
        economics_cost_usd=total,
        audit_signed=True,
        audit_chain_verified=True,
        children=children,
    )


def test_reconciles_one_parent_and_seals_v3_evidence(tmp_path: Path) -> None:
    spend = Decimal("0.00406632")
    plan = subject.single_parent_plan(
        campaign_id="evaluation-studio-v1",
        campaign_spend_before_usd=spend,
    )
    result = subject.reconcile_single_parent(plan, _parent(spend))
    destination = tmp_path / "v3-w2"
    subject.seal_result(
        result=result,
        preflight=subject.V3Preflight("a" * 64, "b" * 64, "c" * 64),
        destination=destination,
    )

    assert (destination / "SHA256SUMS").is_file()
    acceptance = json.loads((destination / "acceptance.json").read_text())
    assert {
        item["criterion_id"]: item["status"] for item in acceptance["criteria"]
    } == {
        "workflow2.happy-3": "pass",
        "workflow2.aggregate-economics": "pass",
    }
    reconciliation = json.loads(
        (destination / "reconciliation" / "workflow2-repetition-3.json").read_text()
    )
    assert reconciliation["configured_repetitions"] == 1
    assert reconciliation["parent_observations"][0]["repetition"] == 3


def test_execute_orchestrates_only_one_parent_after_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spend = Decimal("0.00406632")
    prepared = SimpleNamespace(
        campaign=SimpleNamespace(
            campaign_id="evaluation-studio-v1",
            tenant_id="evaluation-studio-v1",
        ),
        wiring=SimpleNamespace(econ_database=tmp_path / "econ.sqlite3"),
    )
    preflight_result = subject.V3Preflight("a" * 64, "b" * 64, "c" * 64)
    calls: list[object] = []

    monkeypatch.setattr(subject, "preflight", lambda **_values: preflight_result)

    def fake_prepare(**values):
        calls.append(values)
        return prepared

    monkeypatch.setattr(subject, "prepare_live_batch", fake_prepare)
    monkeypatch.setattr(subject, "_campaign_spend", lambda *_args, **_kwargs: spend)

    async def fake_submit(_prepared, plan, _factory):
        calls.append(plan)
        return _parent(spend)

    monkeypatch.setattr(subject, "_submit_one", fake_submit)
    destination = tmp_path / "sealed-v3-w2"
    result = subject.execute(
        profile_path=PROFILE,
        authorization=subject.AUTHORIZATION_PHRASE,
        remediation_bundle=tmp_path / "w1",
        prior_w2_bundle=tmp_path / "w2",
        campaign_config=tmp_path / "campaign.json",
        readiness_attestation=tmp_path / "readiness.json",
        wiring_config=tmp_path / "wiring.json",
        service_api_key_file=tmp_path / "service.key",
        destination=destination,
        environment={},
    )

    assert result == destination
    plans = [value for value in calls if hasattr(value, "submissions")]
    assert len(plans) == 1
    assert [item.repetition for item in plans[0].submissions] == [3]
    assert (destination / "SHA256SUMS").is_file()
