from __future__ import annotations

import json
import sqlite3
import stat
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from release.live_evaluation.config import CampaignConfig
from release.live_evaluation.live_provider_gate import (
    ARM_ENVIRONMENT_VARIABLE,
    ProviderFreeWiring,
)
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
    ServiceCallObservation,
    ServiceOutcomeObservation,
    ServiceRightsizingCapture,
)
from release.live_evaluation.rightsizing_live_driver import (
    RightsizingExecutionContract,
    RightsizingDriverBlockedError,
    build_parser,
    execute,
)
import release.live_evaluation.rightsizing_live_driver as subject
from release.live_evaluation.rightsizing_service_adapter import ExperimentRequest


CAMPAIGN = "evaluation-studio-v1"
RUN = "rightsizing:run-1"
CASES_SHA256 = "039f80f5e2d037c1cd0bd7a5e9edd37522221d61c133f59aaa10e8b63499416a"


def _campaign(tmp_path: Path) -> CampaignConfig:
    root = tmp_path / "artifacts"
    root.mkdir()
    sink = root / "actions"
    sink.mkdir()
    return CampaignConfig.model_validate(
        {
            "schema_version": 1,
            "campaign_id": CAMPAIGN,
            "tenant_id": CAMPAIGN,
            "provider": "openai",
            "model": "openai/gpt-4o-mini",
            "embedding_model": "openai/text-embedding-3-small",
            "vector_backend": "chroma",
            "campaign_budget_usd": "10.00",
            "per_run_cap_usd": "0.25",
            "provider_secret_ref": "llm.openai",
            "artifact_root": str(root),
            "action_sink_root": str(sink),
        }
    )


def _wiring(tmp_path: Path) -> ProviderFreeWiring:
    service = tmp_path / "service.sqlite3"
    service.touch()
    econ = tmp_path / "econ.sqlite3"
    with sqlite3.connect(econ) as database:
        database.execute(
            "CREATE TABLE cost_reservations (tenant_id TEXT, campaign_id TEXT, "
            "status TEXT, actual_cost_usd TEXT, held_cost_usd TEXT)"
        )
        database.execute(
            "INSERT INTO cost_reservations VALUES (?,?,?,?,?)",
            (CAMPAIGN, CAMPAIGN, "committed", "0.01", "0"),
        )
    action = tmp_path / "actions.sqlite3"
    action.touch()
    provider = tmp_path / "provider.json"
    provider.write_text('{"window_id":"window-1","total_usd":"0.02"}')
    request = ExperimentRequest(
        node_id="research-agent",
        incumbent="openai/gpt-4o-mini",
        instruction="Answer only from supplied context.",
        judge_model="openai/gpt-4o-mini",
        max_candidates=1,
        max_cases=3,
        min_cases=3,
    )
    # These template values are irrelevant to this bounded driver but are part of
    # the already-approved unified wiring type.
    from release.live_evaluation.template_live_rendered_execution import (
        LiveTemplateConfig,
        LiveTemplateFixture,
    )

    return ProviderFreeWiring(
        service_base_url="http://127.0.0.1:8122",
        service_database=service,
        econ_database=econ,
        action_sink_database=action,
        provider_window=provider,
        batch_items=(),
        template_config=LiveTemplateConfig(
            fixture_id="fixture-1",
            tenant_id=CAMPAIGN,
            template_name="template-1",
            deployment_ref="deployment-1",
        ),
        template_fixture=LiveTemplateFixture(
            fixture_id="fixture-1",
            template_name="template-1",
            template_version=1,
            workflow_id="workflow-1",
            graph_version_ref="workflow-1@1",
            deployment_ref="deployment-1",
            deployment_version=1,
            provider_calls_performed=0,
        ),
        rightsizing_request=request,
        rightsizing_cases_sha256=CASES_SHA256,
    )


def _reconciliation() -> ReconciliationInput:
    amount = Decimal("0.002")
    return ReconciliationInput(
        audits=(
            AuditRecord(
                audit_event_id="audit-1",
                operation_id="operation-1",
                run_id=RUN,
                cost_event_id="cost-1",
                provider_request_id="provider-1",
                cost_usd=amount,
                cache_hit=False,
                run_status="succeeded",
                signed=True,
                chain_verified=True,
            ),
        ),
        reservations=(
            ReservationRecord(
                reservation_id="reservation-1",
                operation_id="operation-1",
                run_id=RUN,
                state="committed",
                maximum_usd=Decimal("0.25"),
                retained_usd=Decimal("0"),
            ),
        ),
        local_cost_events=(
            LocalCostEvent(
                cost_event_id="cost-1",
                audit_event_id="audit-1",
                operation_id="operation-1",
                run_id=RUN,
                provider_request_id="provider-1",
                amount_usd=amount,
                cache_hit=False,
                run_status="succeeded",
                failure_tax_usd=Decimal("0"),
            ),
        ),
        regulus_events=(
            RegulusExecutionEvent(
                execution_event_id="cost-1",
                cost_event_id="cost-1",
                audit_event_id="audit-1",
                operation_id="operation-1",
                run_id=RUN,
                provider_request_id="provider-1",
                amount_usd=amount,
                failure_tax_usd=Decimal("0"),
                valuation_recorded=False,
                value_usd=Decimal("0"),
                margin_usd=Decimal("0"),
            ),
        ),
        action_receipts=(),
        provider_window=ProviderWindowSummary(window_id="window-1", total_usd=Decimal("0.02")),
    )


def _capture() -> ServiceRightsizingCapture:
    amount = Decimal("0.002")
    return ServiceRightsizingCapture(
        campaign_id=CAMPAIGN,
        cases_sha256=CASES_SHA256,
        run_id=RUN,
        node_id="research-agent",
        mode="equivalence",
        cases=3,
        min_cases=3,
        verdict="confirmed",
        recommended_model="openai/gpt-4.1-nano",
        calls=(
            ServiceCallObservation(
                operation_id="operation-1",
                provider_request_id="provider-1",
                cost_event_id="cost-1",
                audit_event_id="audit-1",
                model="openai/gpt-4.1-nano",
                cost_measurement="measured",
                measured_cost_usd=amount,
                estimated_cost_usd=amount,
                input_tokens=20,
                output_tokens=4,
                cleanup_status="complete",
            ),
        ),
        outcomes=(
            ServiceOutcomeObservation("gpt-4o-mini", "openai", True, 3, 0, False),
            ServiceOutcomeObservation("gpt-4.1-nano", "openai", False, 3, 0, True),
        ),
        response_measured_cost_usd=amount,
        response_estimated_cost_usd=amount,
        reconciliation=_reconciliation(),
        prior_campaign_spend_usd=Decimal("0.01"),
    )


class _Adapter:
    def __init__(self, capture: ServiceRightsizingCapture) -> None:
        self.capture = capture
        self.calls: list[dict[str, object]] = []

    def collect(self, **values):
        self.calls.append(values)
        # Prove the driver supplies a usable service-key source without retaining it.
        assert values["auth_source"]() == "service-only-secret"
        assert values["provider_ready"]() is True
        return self.capture


def _private_key(tmp_path: Path) -> Path:
    path = tmp_path / "private-service-key"
    path.write_text("service-only-secret\n")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return path


def test_v3_one_case_contract_is_explicitly_flagged_and_four_calls(tmp_path: Path) -> None:
    campaign = _campaign(tmp_path)
    wiring = _wiring(tmp_path)
    wiring = replace(
        wiring,
        rightsizing_request=replace(
            wiring.rightsizing_request,
            node_id="analyze",
            max_cases=1,
            min_cases=5,
        ),
    )
    contract = RightsizingExecutionContract(
        node_id="analyze",
        cases_sha256=CASES_SHA256,
        max_cases=1,
        min_cases=5,
        expected_provider_calls=4,
        required_verdict="flagged",
    )

    subject._assert_contract(campaign, wiring, contract=contract)
    capture = replace(
        _capture(),
        node_id="analyze",
        cases=1,
        min_cases=5,
        verdict="flagged",
        calls=tuple(replace(_capture().calls[0], operation_id=f"operation-{index}") for index in range(4)),
    )
    subject._assert_capture_contract(capture, contract)

    with pytest.raises(RightsizingDriverBlockedError, match="rightsizing_call_count_invalid"):
        subject._assert_capture_contract(replace(capture, calls=capture.calls[:3]), contract)


def test_exact_interlocks_fail_before_service_key_database_or_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = _campaign(tmp_path)
    wiring = _wiring(tmp_path)
    key = _private_key(tmp_path)
    key.unlink()
    adapter = _Adapter(_capture())
    database_reads = 0

    def forbidden_connect(*_args, **_kwargs):
        nonlocal database_reads
        database_reads += 1
        pytest.fail("interlocks must gate before database reads")

    monkeypatch.setattr(sqlite3, "connect", forbidden_connect)
    with pytest.raises(RightsizingDriverBlockedError, match="live_execution_not_armed"):
        execute(
            campaign=campaign,
            wiring=wiring,
            service_api_key_file=key,
            output=tmp_path / "observation.json",
            arm="yes",
            environment={ARM_ENVIRONMENT_VARIABLE: CAMPAIGN},
            adapter=adapter,
        )
    with pytest.raises(RightsizingDriverBlockedError, match="live_environment_not_armed"):
        execute(
            campaign=campaign,
            wiring=wiring,
            service_api_key_file=key,
            output=tmp_path / "observation.json",
            arm=ARM_PHRASE,
            environment={},
            adapter=adapter,
        )
    assert database_reads == 0
    assert adapter.calls == []


def test_executes_approved_request_and_writes_sanitized_reconciled_observation(
    tmp_path: Path,
) -> None:
    campaign = _campaign(tmp_path)
    wiring = _wiring(tmp_path)
    adapter = _Adapter(_capture())
    output = tmp_path / "rightsizing-observation.json"

    result = execute(
        campaign=campaign,
        wiring=wiring,
        service_api_key_file=_private_key(tmp_path),
        output=output,
        arm=ARM_PHRASE,
        environment={ARM_ENVIRONMENT_VARIABLE: CAMPAIGN},
        adapter=adapter,
    )

    assert result == output.resolve()
    assert len(adapter.calls) == 1
    submitted = adapter.calls[0]
    assert submitted["request"] == wiring.rightsizing_request
    assert submitted["tenant_id"] == CAMPAIGN
    assert submitted["prior_campaign_spend_usd"] == Decimal("0.01")
    assert submitted["arm"] == ARM_PHRASE
    payload = json.loads(output.read_text())
    assert payload["status"] == "verified"
    assert payload["criteria"] == {
        "rightsizing.cost-reconciliation": "pass",
        "rightsizing.measured-experiment": "pass",
    }
    assert payload["economics"]["experiment_replay_and_judge_total_usd"] == "0.002"
    assert payload["economics"]["audit_total_usd"] == "0.002"
    assert payload["economics"]["regulus_total_usd"] == "0.002"
    assert payload["economics"]["campaign_spend_after_usd"] == "0.012"
    assert payload["economics"]["role_attribution"] == "public_endpoint_unavailable"
    rendered = output.read_text().lower()
    assert "service-only-secret" not in rendered
    assert "api_key" not in rendered
    assert payload["reconciliation"]["reservations"][0]["state"] == "committed"


def test_post_capture_campaign_cap_is_rechecked_before_observation_write(tmp_path: Path) -> None:
    campaign = _campaign(tmp_path)
    wiring = _wiring(tmp_path)
    with sqlite3.connect(wiring.econ_database) as database:
        database.execute(
            "UPDATE cost_reservations SET actual_cost_usd = '9.74' WHERE campaign_id = ?",
            (CAMPAIGN,),
        )
    over = replace(
        _capture(),
        prior_campaign_spend_usd=Decimal("9.74"),
        response_measured_cost_usd=Decimal("0.27"),
        response_estimated_cost_usd=Decimal("0.27"),
        calls=(
            replace(
                _capture().calls[0],
                measured_cost_usd=Decimal("0.27"),
                estimated_cost_usd=Decimal("0.27"),
            ),
        ),
    )
    output = tmp_path / "observation.json"

    with pytest.raises(ValueError, match="per-run cap|campaign cap"):
        execute(
            campaign=campaign,
            wiring=wiring,
            service_api_key_file=_private_key(tmp_path),
            output=output,
            arm=ARM_PHRASE,
            environment={ARM_ENVIRONMENT_VARIABLE: CAMPAIGN},
            adapter=_Adapter(over),
        )
    assert not output.exists()


def test_cli_accepts_service_key_file_but_no_provider_key_or_raw_credential() -> None:
    options = {
        option
        for action in build_parser()._actions
        for option in action.option_strings
    }
    assert options == {
        "-h",
        "--help",
        "--campaign-config",
        "--wiring-config",
        "--service-api-key-file",
        "--arm",
        "--output",
    }
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            [
                "--campaign-config",
                "campaign.json",
                "--wiring-config",
                "wiring.json",
                "--service-api-key-file",
                "/private/service-key",
                "--arm",
                ARM_PHRASE,
                "--output",
                "observation.json",
                "--provider-api-key-file",
                "/private/provider-key",
            ]
        )
