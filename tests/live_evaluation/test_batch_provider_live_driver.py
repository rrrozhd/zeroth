from __future__ import annotations

import json
import os
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
import httpx

from release.live_evaluation.batch_provider_economics import (
    BatchEconomicsObservation,
    ParentBatchObservation,
)
from release.live_evaluation.batch_provider_service_adapter import ARM_PHRASE
from release.live_evaluation.live_provider_gate import ARM_ENVIRONMENT_VARIABLE

import release.live_evaluation.batch_provider_live_driver as subject


CAMPAIGN = "evaluation-studio-v1"
SERVICE_SECRET = "ephemeral-zeroth-service-secret"


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    campaign = tmp_path / "campaign.json"
    campaign.write_text(
        json.dumps(
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
                "artifact_root": str(artifact_root),
                "action_sink_root": str(artifact_root / "action-sink"),
            }
        ),
        encoding="utf-8",
    )
    readiness = tmp_path / "readiness.json"
    readiness.write_text(
        json.dumps(
            {
                "campaign_id": CAMPAIGN,
                "tenant_id": CAMPAIGN,
                "logical_secret_ref": "llm.openai",
                "installed": True,
                "provider_probe_reconciled": True,
                "provider_request_id": "readiness-provider",
                "operation_id": "readiness-operation",
                "run_id": "readiness-run",
                "audit_event_id": "readiness-audit",
                "cost_event_id": "readiness-cost",
                "measured_cost_usd": "0.000001",
                "campaign_spend_before_usd": "0.000001",
                "audit_signed": True,
            }
        ),
        encoding="utf-8",
    )
    persisted: dict[str, str] = {}
    for name in (
        "service_database",
        "econ_database",
        "action_sink_database",
        "provider_window",
    ):
        path = tmp_path / f"{name}.fixture"
        path.write_bytes(b"approved-persistent-plane")
        persisted[name] = str(path)
    wiring = tmp_path / "wiring.json"
    wiring.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "service_base_url": "http://127.0.0.1:8122",
                **persisted,
                "batch_items": [
                    {"index": index, "query": f"real world query number {index}"}
                    for index in range(8)
                ],
                "template": {
                    "fixture_id": "template-render-20260826",
                    "tenant_id": CAMPAIGN,
                    "template_name": "live-render-template-20260826",
                    "deployment_ref": "live-render-template-20260826-v1",
                    "template_version": 1,
                    "workflow_id": "workflow-template-live",
                    "graph_version_ref": "workflow-template-live@1",
                    "deployment_version": 1,
                    "provider_calls_performed": 0,
                },
                "rightsizing": {
                    "cases_sha256": "a" * 64,
                    "node_id": "research-agent",
                    "incumbent": "openai/gpt-4o-mini",
                    "instruction": "Answer only from supplied context.",
                    "needs_tools": False,
                    "needs_vision": False,
                    "judge_model": "openai/gpt-4o-mini",
                    "max_candidates": 1,
                    "max_cases": 1,
                    "min_cases": 1,
                    "tolerance_pct": 5.0,
                    "mode": "equivalence",
                },
            }
        ),
        encoding="utf-8",
    )
    service_key = tmp_path / "service.key"
    service_key.write_text(SERVICE_SECRET + "\n", encoding="utf-8")
    service_key.chmod(0o600)
    return campaign, readiness, wiring, service_key


def _parent(repetition: int) -> ParentBatchObservation:
    actual = Decimal("0.000020")
    parent_id = f"parent-{repetition}"
    children = tuple(
        BatchEconomicsObservation(
            campaign_id=CAMPAIGN,
            repetition=repetition,
            item_index=index,
            parent_run_id=parent_id,
            child_run_id=f"child-{repetition}-{index}",
            operation_id=f"operation-{repetition}-{index}",
            provider_request_id=f"provider-{repetition}-{index}",
            audit_event_id=f"audit-{repetition}-{index}",
            cost_event_ids=(f"cost-{repetition}-{index}",),
            reservation_id=f"reservation-{repetition}-{index}",
            reservation_operation_id=f"operation-{repetition}-{index}",
            reservation_status="committed",
            reserved_max_cost_usd=Decimal("0.000025"),
            reservation_actual_cost_usd=actual,
            reservation_released_cost_usd=Decimal("0.000005"),
            reservation_cleanup_status="complete",
            cache_hit=False,
            audit_cost_usd=actual,
            run_cost_usd=actual,
            local_cost_usd=actual,
            economics_cost_usd=actual,
            audit_signed=True,
            audit_chain_verified=True,
            parent_child_linked=True,
        )
        for index in range(8)
    )
    total = actual * 8
    return ParentBatchObservation(
        campaign_id=CAMPAIGN,
        repetition=repetition,
        parent_run_id=parent_id,
        status="succeeded",
        configured_concurrency=4,
        observed_peak_concurrency=4,
        campaign_spend_after_usd=Decimal("0.000001") + total * repetition,
        audit_cost_usd=total,
        run_cost_usd=total,
        local_cost_usd=total,
        economics_cost_usd=total,
        audit_signed=True,
        audit_chain_verified=True,
        children=children,
    )


class _Adapter:
    def __init__(self, auth_source) -> None:
        self.auth_source = auth_source
        self.repetitions: list[int] = []

    async def submit_parent(self, submission):
        assert self.auth_source() == SERVICE_SECRET
        self.repetitions.append(submission.repetition)
        return _parent(submission.repetition)


def test_executes_exact_plan_and_writes_exclusive_sanitized_observation(
    tmp_path: Path,
) -> None:
    campaign, readiness, wiring, service_key = _inputs(tmp_path)
    output = tmp_path / "observation.json"
    adapters: list[_Adapter] = []

    def factory(_prepared, auth_source):
        adapter = _Adapter(auth_source)
        adapters.append(adapter)
        return adapter

    result = subject.execute(
        campaign_config=campaign,
        readiness_attestation=readiness,
        wiring_config=wiring,
        service_api_key_file=service_key,
        output=output,
        arm=ARM_PHRASE,
        environment={ARM_ENVIRONMENT_VARIABLE: CAMPAIGN},
        adapter_factory=factory,
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert result == output
    assert adapters[0].repetitions == [1, 2, 3]
    assert payload["criterion_id"] == "batching.provider-economics"
    assert payload["passed"] is True
    assert payload["configured_repetitions"] == 3
    assert payload["configured_items_per_repetition"] == 8
    assert payload["configured_concurrency"] == 4
    assert payload["per_run_cap_usd"] == "0.25"
    assert payload["campaign_cap_usd"] == "10.00"
    assert len(payload["parent_observations"]) == 3
    serialized = output.read_text(encoding="utf-8")
    assert SERVICE_SECRET not in serialized
    assert "api_key" not in serialized.lower()

    with pytest.raises(FileExistsError):
        subject.execute(
            campaign_config=campaign,
            readiness_attestation=readiness,
            wiring_config=wiring,
            service_api_key_file=service_key,
            output=output,
            arm=ARM_PHRASE,
            environment={ARM_ENVIRONMENT_VARIABLE: CAMPAIGN},
            adapter_factory=factory,
        )


@pytest.mark.parametrize(
    ("arm", "environment", "code"),
    (
        ("wrong", {ARM_ENVIRONMENT_VARIABLE: CAMPAIGN}, "operator_interlock_invalid"),
        (ARM_PHRASE, {}, "operator_interlock_invalid"),
    ),
)
def test_operator_interlocks_fail_before_service_key_read_or_adapter_creation(
    tmp_path: Path,
    arm: str,
    environment: dict[str, str],
    code: str,
) -> None:
    campaign, readiness, wiring, service_key = _inputs(tmp_path)
    service_key.unlink()

    with pytest.raises(subject.BatchProviderLiveBlocked) as caught:
        subject.execute(
            campaign_config=campaign,
            readiness_attestation=readiness,
            wiring_config=wiring,
            service_api_key_file=service_key,
            output=tmp_path / "observation.json",
            arm=arm,
            environment=environment,
            adapter_factory=lambda *_args: pytest.fail("adapter must not be created"),
        )

    assert caught.value.code == code


def test_rejects_non_private_service_key_before_adapter_creation(tmp_path: Path) -> None:
    campaign, readiness, wiring, service_key = _inputs(tmp_path)
    service_key.chmod(0o644)

    with pytest.raises(subject.BatchProviderLiveBlocked) as caught:
        subject.execute(
            campaign_config=campaign,
            readiness_attestation=readiness,
            wiring_config=wiring,
            service_api_key_file=service_key,
            output=tmp_path / "observation.json",
            arm=ARM_PHRASE,
            environment={ARM_ENVIRONMENT_VARIABLE: CAMPAIGN},
            adapter_factory=lambda *_args: pytest.fail("adapter must not be created"),
        )

    assert caught.value.code == "service_key_file_not_private"


def test_campaign_caps_are_revalidated_by_execution_driver(tmp_path: Path) -> None:
    campaign, readiness, wiring, service_key = _inputs(tmp_path)
    payload = json.loads(campaign.read_text(encoding="utf-8"))
    payload["per_run_cap_usd"] = "0.26"
    campaign.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(subject.BatchProviderLiveBlocked) as caught:
        subject.execute(
            campaign_config=campaign,
            readiness_attestation=readiness,
            wiring_config=wiring,
            service_api_key_file=service_key,
            output=tmp_path / "observation.json",
            arm=ARM_PHRASE,
            environment={ARM_ENVIRONMENT_VARIABLE: CAMPAIGN},
            adapter_factory=lambda *_args: pytest.fail("adapter must not be created"),
        )

    assert caught.value.code == "campaign_configuration_invalid"


def test_cli_failure_is_stable_and_never_echoes_argument_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    campaign, readiness, wiring, service_key = _inputs(tmp_path)
    sentinel = "sensitive-value-that-must-not-echo"

    exit_code = subject.main(
        [
            "--campaign-config",
            str(campaign),
            "--readiness-attestation",
            str(readiness),
            "--wiring-config",
            str(wiring),
            "--service-api-key-file",
            str(service_key),
            "--output",
            str(tmp_path / "observation.json"),
            "--arm",
            sentinel,
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert json.loads(captured.out) == {
        "completed": False,
        "provider_calls_performed": 0,
        "reason": "operator_interlock_invalid",
    }
    assert sentinel not in captured.out + captured.err


def test_no_provider_credential_input_surface_exists() -> None:
    destinations = {
        action.dest for action in subject.build_parser()._actions if action.dest != "help"
    }
    assert not destinations & {
        "api_key",
        "provider_api_key",
        "provider_key_file",
        "openai_api_key",
    }
    assert "service_api_key_file" in destinations


def test_public_audit_source_binds_campaign_identity_from_run_evidence() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/evidence"):
            return httpx.Response(
                200,
                json={
                    "run": {
                        "run_id": "child-1",
                        "campaign_id": CAMPAIGN,
                    },
                    "audits": [{"audit_id": "audit-1", "run_id": "child-1"}],
                },
            )
        return httpx.Response(
            200,
            json={
                "verified": True,
                "signature_verified": True,
                "unsigned_record_count": 0,
            },
        )

    source = subject._PublicRunAuditSource(
        base_url="http://127.0.0.1:8122",
        tenant_id=CAMPAIGN,
        campaign_id=CAMPAIGN,
        auth_source=lambda: SERVICE_SECRET,
    )
    source._client.close()
    source._client = httpx.Client(transport=httpx.MockTransport(handler))

    verification = source.verify_run("child-1")

    assert verification["run_id"] == "child-1"
    assert verification["tenant_id"] == CAMPAIGN
    assert verification["campaign_id"] == CAMPAIGN
    assert source.records_for_runs(("child-1",))[0]["audit_id"] == "audit-1"
    assert len(requests) == 2
    assert all(request.headers["X-API-Key"] == SERVICE_SECRET for request in requests)
    source.close()


def test_exact_service_secret_cannot_escape_through_an_authoritative_identity(
    tmp_path: Path,
) -> None:
    campaign, readiness, wiring, service_key = _inputs(tmp_path)
    output = tmp_path / "observation.json"

    class LeakingAdapter(_Adapter):
        async def submit_parent(self, submission):
            parent = await super().submit_parent(submission)
            children = list(parent.children)
            if submission.repetition == 1:
                children[0] = replace(children[0], provider_request_id=SERVICE_SECRET)
            return replace(parent, children=tuple(children))

    with pytest.raises(subject.BatchProviderLiveBlocked) as caught:
        subject.execute(
            campaign_config=campaign,
            readiness_attestation=readiness,
            wiring_config=wiring,
            service_api_key_file=service_key,
            output=output,
            arm=ARM_PHRASE,
            environment={ARM_ENVIRONMENT_VARIABLE: CAMPAIGN},
            adapter_factory=lambda _prepared, auth_source: LeakingAdapter(auth_source),
        )

    assert caught.value.code == "observation_contains_service_credential"
    assert not output.exists()
