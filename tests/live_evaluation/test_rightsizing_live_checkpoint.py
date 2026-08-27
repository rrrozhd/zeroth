from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from release.live_evaluation.reconciliation import (
    AuditRecord,
    LocalCostEvent,
    ProviderWindowSummary,
    ReconciliationInput,
    RegulusExecutionEvent,
    ReservationRecord,
)
from release.live_evaluation.rightsizing_live_checkpoint import (
    ARM_PHRASE,
    MODEL,
    CallObservation,
    QualityVerdictObservation,
    RightsizingCapture,
    RightsizingLiveHarness,
    UsageObservation,
    capture_service_response,
    load_recorded_cases,
    seal_capture,
    validate_service_capture,
    validate_capture,
)


class _Catalog:
    def __init__(self, present: bool) -> None:
        self.present = present
        self.lookups: list[str] = []

    def has_reference(self, logical_name: str) -> bool:
        self.lookups.append(logical_name)
        return self.present


class _Executor:
    def __init__(self, observations: list[CallObservation]) -> None:
        self.observations = iter(observations)
        self.calls: list[tuple[str, str, str]] = []

    def execute(self, *, case, role, authorization):
        self.calls.append((case.case_id, role, authorization.credential_reference))
        return next(self.observations)


def _write_cases(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "cases": [
                    {
                        "case_id": "refund-policy",
                        "input": {"request": "Summarize the refund rule."},
                        "reference": "Refunds are available for 30 days.",
                    },
                    {
                        "case_id": "escalation-policy",
                        "input": {"request": "When should this be escalated?"},
                        "reference": "Escalate after two failed attempts.",
                    },
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _call(case_id: str, role: str, index: int) -> CallObservation:
    return CallObservation(
        case_id=case_id,
        role=role,
        model=MODEL,
        provider_request_id=f"provider-{index}",
        usage=UsageObservation(
            input_tokens=100 + index, output_tokens=20, total_tokens=120 + index
        ),
        measured_cost_usd=Decimal("0.000100"),
        estimated_cost_usd=Decimal("0.000110"),
        cost_event_id=f"cost-{index}",
        audit_event_id=f"audit-{index}",
        operation_id=f"operation-{index}",
        run_id=f"run-{index}",
    )


def _capture() -> RightsizingCapture:
    calls = tuple(
        _call(case_id, role, index)
        for index, (case_id, role) in enumerate(
            (
                ("refund-policy", "incumbent"),
                ("refund-policy", "candidate"),
                ("refund-policy", "judge"),
                ("escalation-policy", "incumbent"),
                ("escalation-policy", "candidate"),
                ("escalation-policy", "judge"),
            ),
            start=1,
        )
    )
    audits = tuple(
        AuditRecord(
            audit_event_id=call.audit_event_id,
            operation_id=call.operation_id,
            run_id=call.run_id,
            cost_event_id=call.cost_event_id,
            provider_request_id=call.provider_request_id,
            cost_usd=call.measured_cost_usd,
            cache_hit=False,
            run_status="succeeded",
            signed=True,
            chain_verified=True,
        )
        for call in calls
    )
    local = tuple(
        LocalCostEvent(
            cost_event_id=call.cost_event_id,
            audit_event_id=call.audit_event_id,
            operation_id=call.operation_id,
            run_id=call.run_id,
            provider_request_id=call.provider_request_id,
            amount_usd=call.measured_cost_usd,
            cache_hit=False,
            run_status="succeeded",
            failure_tax_usd=Decimal("0"),
        )
        for call in calls
    )
    regulus = tuple(
        RegulusExecutionEvent(
            execution_event_id=f"regulus-{index}",
            cost_event_id=call.cost_event_id,
            audit_event_id=call.audit_event_id,
            operation_id=call.operation_id,
            run_id=call.run_id,
            provider_request_id=call.provider_request_id,
            amount_usd=call.measured_cost_usd,
            failure_tax_usd=Decimal("0"),
            valuation_recorded=False,
            value_usd=Decimal("0"),
            margin_usd=Decimal("0"),
        )
        for index, call in enumerate(calls, start=1)
    )
    return RightsizingCapture(
        campaign_id="rightsizing-live-20260826",
        cases_sha256="a" * 64,
        calls=calls,
        verdicts=(
            QualityVerdictObservation(
                case_id="refund-policy",
                candidate_request_id="provider-2",
                judge_request_id="provider-3",
                score=Decimal("0.95"),
                passed=True,
                persistence_id="quality-1",
                persisted=True,
                readback_verified=True,
            ),
            QualityVerdictObservation(
                case_id="escalation-policy",
                candidate_request_id="provider-5",
                judge_request_id="provider-6",
                score=Decimal("0.90"),
                passed=True,
                persistence_id="quality-2",
                persisted=True,
                readback_verified=True,
            ),
        ),
        reconciliation=ReconciliationInput(
            audits=audits,
            reservations=tuple(
                ReservationRecord(
                    reservation_id=f"reservation-{index}",
                    operation_id=call.operation_id,
                    run_id=call.run_id,
                    state="committed",
                    maximum_usd=Decimal("0.25"),
                    retained_usd=Decimal("0"),
                )
                for index, call in enumerate(calls, start=1)
            ),
            local_cost_events=local,
            regulus_events=regulus,
            action_receipts=(),
            provider_window=ProviderWindowSummary(
                window_id="openai-project-window", total_usd=Decimal("0.01")
            ),
        ),
        prior_campaign_spend_usd=Decimal("0"),
    )


def test_load_recorded_cases_is_replayable_and_checksum_pinned(tmp_path: Path) -> None:
    cases, digest = load_recorded_cases(_write_cases(tmp_path / "cases.json"))

    assert [case.case_id for case in cases] == ["refund-policy", "escalation-policy"]
    assert len(digest) == 64
    assert load_recorded_cases(tmp_path / "cases.json")[1] == digest


def test_dry_run_fails_closed_without_logical_openai_reference(tmp_path: Path) -> None:
    harness = RightsizingLiveHarness(secret_catalog=_Catalog(False))

    plan = harness.readiness(_write_cases(tmp_path / "cases.json"))

    assert plan.ready is False
    assert plan.provider_calls_performed == 0
    assert "llm.openai" in plan.blockers[0]


def test_execution_requires_exact_arm_phrase_and_never_resolves_secret_value(
    tmp_path: Path,
) -> None:
    catalog = _Catalog(True)
    executor = _Executor(list(_capture().calls))
    harness = RightsizingLiveHarness(secret_catalog=catalog)
    cases_file = _write_cases(tmp_path / "cases.json")

    with pytest.raises(PermissionError, match="explicitly armed"):
        harness.run(
            cases_file=cases_file,
            executor=executor,
            arm="yes",
            prior_campaign_spend_usd=Decimal("0"),
        )

    observations = harness.run(
        cases_file=cases_file,
        executor=executor,
        arm=ARM_PHRASE,
        prior_campaign_spend_usd=Decimal("0"),
    )

    assert len(observations) == 6
    assert catalog.lookups == ["llm.openai", "llm.openai"]
    assert all(call[2] == "llm.openai" for call in executor.calls)


def test_execution_reserves_worst_case_campaign_capacity_before_provider_call(
    tmp_path: Path,
) -> None:
    executor = _Executor(list(_capture().calls))
    harness = RightsizingLiveHarness(secret_catalog=_Catalog(True))

    with pytest.raises(PermissionError, match="remaining campaign capacity"):
        harness.run(
            cases_file=_write_cases(tmp_path / "cases.json"),
            executor=executor,
            arm=ARM_PHRASE,
            prior_campaign_spend_usd=Decimal("9.00"),
        )

    assert executor.calls == []


def test_capture_validates_exact_calls_quality_persistence_and_budget() -> None:
    summary = validate_capture(_capture())

    assert summary.measured_total_usd == Decimal("0.000600")
    assert summary.estimated_total_usd == Decimal("0.000660")
    assert summary.quality_pass_rate == Decimal("1")
    assert summary.provider_window_policy == "upper_bound_only"


def test_capture_fails_when_quality_is_not_read_back_or_cost_planes_diverge() -> None:
    baseline = _capture()
    bad_verdict = replace(baseline.verdicts[0], readback_verified=False)
    with pytest.raises(ValueError, match="persisted and read back"):
        validate_capture(replace(baseline, verdicts=(bad_verdict, baseline.verdicts[1])))

    bad_local = replace(baseline.reconciliation.local_cost_events[0], amount_usd=Decimal("0.01"))
    with pytest.raises(ValueError, match="reconciliation"):
        validate_capture(
            replace(
                baseline,
                reconciliation=replace(
                    baseline.reconciliation,
                    local_cost_events=(bad_local, *baseline.reconciliation.local_cost_events[1:]),
                ),
            )
        )


def test_shared_provider_project_window_is_only_an_upper_bound() -> None:
    baseline = _capture()
    noisy = replace(
        baseline.reconciliation,
        provider_window=ProviderWindowSummary(
            window_id="shared-noisy-window", total_usd=Decimal("9.75")
        ),
    )

    assert validate_capture(replace(baseline, reconciliation=noisy)).provider_window_policy == (
        "upper_bound_only"
    )


def test_permission_blocked_provider_window_keeps_local_reconciliation_explicit() -> None:
    baseline = _capture()
    unavailable = replace(
        baseline.reconciliation,
        provider_window=ProviderWindowSummary(
            window_id="unavailable:provider-usage-403",
            total_usd=Decimal("0"),
        ),
    )

    summary = validate_capture(replace(baseline, reconciliation=unavailable))

    assert summary.provider_window_policy == "unavailable_campaign_local_only"


def test_real_service_response_is_captured_without_inventing_call_roles() -> None:
    baseline = _capture()
    service_run_id = "rightsizing:service-run-1"
    service_reconciliation = replace(
        baseline.reconciliation,
        audits=tuple(
            replace(item, run_id=service_run_id) for item in baseline.reconciliation.audits
        ),
        reservations=tuple(
            replace(item, run_id=service_run_id) for item in baseline.reconciliation.reservations
        ),
        local_cost_events=tuple(
            replace(item, run_id=service_run_id)
            for item in baseline.reconciliation.local_cost_events
        ),
        regulus_events=tuple(
            replace(item, run_id=service_run_id) for item in baseline.reconciliation.regulus_events
        ),
    )
    response = {
        "incumbent": "openai/gpt-4o-mini",
        "node_id": "research",
        "mode": "equivalence",
        "cases": 2,
        "min_cases": 2,
        "tolerance_pct": 5,
        "incumbent_self_equivalence": 1,
        "mean_input_tokens": 108,
        "mean_output_tokens": 20,
        "token_profile_measured": True,
        "harvest": {"cases": 2},
        "outcomes": [
            {
                "model": "gpt-4o-mini",
                "provider": "openai",
                "is_incumbent": True,
                "cases_evaluated": 2,
                "cases_errored": 0,
                "meets_bar": False,
            },
            {
                "model": "gpt-4.1-nano",
                "provider": "openai",
                "is_incumbent": False,
                "cases_evaluated": 2,
                "cases_errored": 0,
                "meets_bar": True,
            },
        ],
        "recommended_model": "openai/gpt-4.1-nano",
        "verdict": "confirmed",
        "note": "Measured on two recorded cases.",
        "execution": {
            "run_id": service_run_id,
            "campaign_id": baseline.campaign_id,
            "provider_call_count": len(baseline.calls),
            "measured_cost_usd": "0.000600",
            "estimated_cost_usd": "0.000660",
            "calls": [
                {
                    "operation_id": call.operation_id,
                    "provider_request_id": call.provider_request_id,
                    "cost_event_id": call.cost_event_id,
                    "audit_event_id": call.audit_event_id,
                    "model": (
                        "openai/gpt-4.1-nano" if call.role == "candidate" else "openai/gpt-4o-mini"
                    ),
                    "cost_measurement": "measured",
                    "measured_cost_usd": format(call.measured_cost_usd, "f"),
                    "estimated_cost_usd": format(call.estimated_cost_usd, "f"),
                    "input_tokens": call.usage.input_tokens,
                    "output_tokens": call.usage.output_tokens,
                    "cleanup_status": "complete",
                    "provider_call_attempted": True,
                    "cache_hit": False,
                }
                for call in baseline.calls
            ],
        },
    }

    capture = capture_service_response(
        response=response,
        cases_sha256=baseline.cases_sha256,
        prior_campaign_spend_usd=baseline.prior_campaign_spend_usd,
        reconciliation=service_reconciliation,
    )
    summary = validate_service_capture(capture)

    assert capture.run_id == "rightsizing:service-run-1"
    assert {call.model for call in capture.calls} == {
        "openai/gpt-4o-mini",
        "openai/gpt-4.1-nano",
    }
    assert not hasattr(capture.calls[0], "role")
    assert summary.measured_total_usd == Decimal("0.000600")
    assert summary.estimated_total_usd == Decimal("0.000660")
    assert summary.quality_pass_rate == Decimal("1")

    with pytest.raises(ValueError, match="service run identity"):
        validate_service_capture(replace(capture, reconciliation=baseline.reconciliation))


def test_real_service_response_uses_operation_identity_when_provider_request_id_unavailable() -> None:
    baseline = _capture()
    response = {
        "incumbent": "openai/gpt-4o-mini",
        "node_id": "research",
        "mode": "equivalence",
        "cases": 1,
        "min_cases": 1,
        "outcomes": [
            {
                "model": "gpt-4.1-nano",
                "provider": "openai",
                "is_incumbent": False,
                "cases_evaluated": 1,
                "cases_errored": 0,
                "meets_bar": True,
            }
        ],
        "recommended_model": "openai/gpt-4.1-nano",
        "verdict": "confirmed",
        "execution": {
            "run_id": "rightsizing:service-run-2",
            "campaign_id": baseline.campaign_id,
            "provider_call_count": 1,
            "measured_cost_usd": "0.000100",
            "estimated_cost_usd": "0.000110",
            "calls": [
                {
                    "operation_id": "operation-1",
                    "provider_request_id": None,
                    "cost_event_id": "cost-1",
                    "audit_event_id": "audit-1",
                    "model": "openai/gpt-4.1-nano",
                    "cost_measurement": "measured",
                    "measured_cost_usd": "0.000100",
                    "estimated_cost_usd": "0.000110",
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "cleanup_status": "complete",
                    "provider_call_attempted": True,
                    "cache_hit": False,
                }
            ],
        },
    }

    service_run_id = "rightsizing:service-run-2"
    reconciliation = ReconciliationInput(
        audits=(
            replace(
                baseline.reconciliation.audits[0],
                run_id=service_run_id,
                provider_request_id=None,
            ),
        ),
        reservations=(
            replace(baseline.reconciliation.reservations[0], run_id=service_run_id),
        ),
        local_cost_events=(
            replace(
                baseline.reconciliation.local_cost_events[0],
                run_id=service_run_id,
                provider_request_id=None,
            ),
        ),
        regulus_events=(
            replace(
                baseline.reconciliation.regulus_events[0],
                run_id=service_run_id,
                provider_request_id=None,
            ),
        ),
        action_receipts=(),
        provider_window=baseline.reconciliation.provider_window,
    )

    capture = capture_service_response(
        response=response,
        cases_sha256=baseline.cases_sha256,
        prior_campaign_spend_usd=baseline.prior_campaign_spend_usd,
        reconciliation=reconciliation,
    )

    assert capture.calls[0].provider_request_id is None
    assert validate_service_capture(capture).measured_total_usd == Decimal("0.000100")


def test_sealer_emits_exact_criteria_and_secret_clean_checksums(tmp_path: Path) -> None:
    screenshot = tmp_path / "rightsizing.png"
    screenshot.write_bytes(b"\x89PNG\r\n\x1a\nsealed screenshot")
    bundle = tmp_path / "bundle"

    seal_capture(capture=_capture(), screenshot=screenshot, destination=bundle)

    acceptance = json.loads((bundle / "acceptance.json").read_text(encoding="utf-8"))
    assert {item["criterion_id"] for item in acceptance["criteria"]} == {
        "rightsizing.measured-experiment",
        "rightsizing.cost-reconciliation",
    }
    assert {item["status"] for item in acceptance["criteria"]} == {"pass"}
    for relative in (
        "screenshots/rightsizing-live.png",
        "reconciliation/runtime.json",
        "reconciliation/audit.json",
        "reconciliation/economics.json",
        "SHA256SUMS",
    ):
        assert (bundle / relative).is_file()
    text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in bundle.rglob("*")
        if path.is_file()
    )
    assert "sk-proj-" not in text
