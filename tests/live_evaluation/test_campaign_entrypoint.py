from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import release.live_evaluation.campaign_entrypoint as entrypoint_module
from release.live_evaluation.campaign_entrypoint import (
    CampaignEndpoints,
    CampaignEntrypoint,
    compose_local_campaign_controllers,
    main,
    mutation_acknowledgement,
)
from release.live_evaluation.campaign_http import provider_acknowledgement
from release.live_evaluation.campaign_runtime import LiveExecutionOptions
from release.live_evaluation.config import CampaignConfig
from release.live_evaluation.coordinator import CriterionResult, StepResult
from release.live_evaluation.coordinator import Phase
from release.live_evaluation.criteria import original_acceptance_criteria
from release.live_evaluation.evidence import CorrelationIds, EvidenceStore
from release.live_evaluation.ledger import CampaignLedger
from zeroth.integrations.memory.tenant_scoped import tenant_slug


def _campaign(tmp_path: Path) -> tuple[Path, Path, Path]:
    artifact_root = tmp_path / "external"
    sink_root = artifact_root / "action-sink"
    path = tmp_path / "campaign.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "campaign_id": "evaluation-studio-v1",
                "tenant_id": "evaluation-studio-v1",
                "provider": "openai",
                "model": "openai/gpt-4o-mini",
                "embedding_model": "openai/text-embedding-3-small",
                "vector_backend": "chroma",
                "campaign_budget_usd": "10.00",
                "per_run_cap_usd": "0.25",
                "provider_secret_ref": "llm.openai",
                "artifact_root": str(artifact_root),
                "action_sink_root": str(sink_root),
            }
        )
    )
    return path, artifact_root, sink_root


def _endpoints() -> CampaignEndpoints:
    campaign = "evaluation-studio-v1"
    return CampaignEndpoints(
        console_base_url="http://127.0.0.1:8100",
        deployment_base_urls={
            f"{campaign}-grounded-researcher-v1": "http://127.0.0.1:8101",
            f"{campaign}-batched-investigation-child-v1": "http://127.0.0.1:8102",
            f"{campaign}-batched-investigation-parent-v1": "http://127.0.0.1:8103",
            f"{campaign}-governed-remediation-v1": "http://127.0.0.1:8104",
        },
        fault_control_url="http://127.0.0.1:8101",
    )


def _options(artifact_root: Path, sink_root: Path, **updates: object) -> LiveExecutionOptions:
    values = {
        "campaign_id": "evaluation-studio-v1",
        "artifact_root": artifact_root,
        "action_sink_root": sink_root,
    }
    values.update(updates)
    return LiveExecutionOptions(**values)


def _seed_control_bundle(bundle: Path) -> None:
    store = EvidenceStore(bundle)
    snapshot = bundle / "database-snapshots" / "service-pretest.sqlite3"
    snapshot.parent.mkdir(parents=True)
    with sqlite3.connect(snapshot) as connection:
        connection.execute("CREATE TABLE control_snapshot (id INTEGER PRIMARY KEY)")
    store.write_manifest(
        {
            "campaign_config": {"campaign_id": "evaluation-studio-v1"},
            "dirty_tree_hash": "sha256:" + "1" * 64,
            "pretest_sqlite_snapshots": ["database-snapshots/service-pretest.sqlite3"],
            "revision": "deadbeef",
        }
    )
    proof = store.append_event("control.local-proof", {"state": "ready"})
    store.append_event(
        "control.signed-readiness.recorded",
        {"algorithm": "hmac-sha256", "state": "signed"},
    )
    ledger = CampaignLedger(store, original_acceptance_criteria())
    reference = f"events.ndjson#{proof}"
    for criterion in ledger.criteria:
        if criterion.criterion_id.startswith("control.") or criterion.criterion_id == (
            "audit.probe-events-instrumented"
        ):
            ledger.record(criterion.criterion_id, "pass", evidence=(reference,))
    for index, kind in enumerate(("provider", "chroma"), start=1):
        store.append_event(
            "control.probe.authorized",
            {"kind": kind},
            correlation=CorrelationIds(operation_id=f"op-{index}", run_id=f"run-{index}"),
        )
        store.append_event(
            "control.probe.reconciled",
            {
                "audit_chain_signed": True,
                "cache_hit": False,
                "cleanup_state": "committed",
                "connector_request_identity": (f"connector-{index}" if kind == "chroma" else None),
                "kind": kind,
                "request_count": 1,
            },
            correlation=CorrelationIds(
                operation_id=f"op-{index}",
                run_id=f"run-{index}",
                audit_event_id=f"audit-{index}",
                cost_event_id=f"cost-{index}",
                provider_request_id=f"provider-{index}",
            ),
        )


def test_dry_run_is_inert_and_does_not_create_evidence_or_call_factories(tmp_path: Path) -> None:
    path, artifact_root, sink_root = _campaign(tmp_path)
    called = []
    entrypoint = CampaignEntrypoint(
        repository_root=tmp_path,
        campaign_config_path=path,
        evidence_bundle=artifact_root / "bundle",
        endpoints=_endpoints(),
        options=_options(artifact_root, sink_root),
        database_factory=lambda: called.append("database"),
        client_factory=lambda: called.append("client"),
    )

    preview = entrypoint.run(dry_run=True)

    assert preview.mode == "dry-run"
    assert preview.campaign_id == "evaluation-studio-v1"
    assert not (artifact_root / "bundle").exists()
    assert called == []


def test_live_run_requires_exact_mutation_ack_before_any_side_effect(tmp_path: Path) -> None:
    path, artifact_root, sink_root = _campaign(tmp_path)
    called = []
    entrypoint = CampaignEntrypoint(
        repository_root=tmp_path,
        campaign_config_path=path,
        evidence_bundle=artifact_root / "bundle",
        endpoints=_endpoints(),
        options=_options(
            artifact_root,
            sink_root,
            allow_mutation=True,
            mutation_acknowledgement="wrong",
            allow_provider=True,
            paid_acknowledgement=provider_acknowledgement("evaluation-studio-v1"),
        ),
        database_factory=lambda: called.append("database"),
        client_factory=lambda: called.append("client"),
    )

    with pytest.raises(ValueError, match="mutation acknowledgement"):
        entrypoint.run()

    assert called == []
    assert not (artifact_root / "bundle").exists()


def test_live_run_stops_owned_processes_and_records_sanitized_failure(tmp_path: Path) -> None:
    path, artifact_root, sink_root = _campaign(tmp_path)
    _seed_control_bundle(artifact_root / "bundle")

    class Supervisor:
        def __init__(self) -> None:
            self.stopped = False

        def restart(self, *, deployment_ref: str, service_url: str) -> None:
            del deployment_ref, service_url
            raise RuntimeError("raw upstream detail must not be persisted")

        def stop_all(self) -> None:
            self.stopped = True

    supervisor = Supervisor()
    entrypoint = CampaignEntrypoint(
        repository_root=tmp_path,
        campaign_config_path=path,
        evidence_bundle=artifact_root / "bundle",
        endpoints=_endpoints(),
        options=_options(
            artifact_root,
            sink_root,
            allow_mutation=True,
            mutation_acknowledgement=mutation_acknowledgement("evaluation-studio-v1"),
            allow_provider=True,
            paid_acknowledgement=provider_acknowledgement("evaluation-studio-v1"),
        ),
        database_factory=lambda: object(),
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(500, json={"detail": "not ready"})
            )
        ),
        publisher_factory=lambda database: object(),
        supervisor_factory=lambda store: supervisor,
    )

    result = entrypoint.run()

    assert result.mode == "live"
    assert result.summary is not None and not result.summary.completed
    assert supervisor.stopped
    events = (artifact_root / "bundle" / "events.ndjson").read_text()
    assert "raw upstream detail" not in events
    assert "campaign.entrypoint.finished" in events


def test_endpoints_reject_remote_or_incomplete_origins() -> None:
    with pytest.raises(ValueError, match="loopback"):
        CampaignEndpoints(
            console_base_url="https://example.com",
            deployment_base_urls={"deployment": "http://127.0.0.1:8101"},
            fault_control_url="http://127.0.0.1:8101",
        )


def test_terminal_bundle_resumes_without_reopening_runtime_boundaries(tmp_path: Path) -> None:
    path, artifact_root, sink_root = _campaign(tmp_path)
    bundle = artifact_root / "bundle"
    _seed_control_bundle(bundle)
    EvidenceStore(bundle).append_event(
        "campaign.terminated",
        {
            "check_ran": False,
            "completed_steps": ["workflow1.deployment-gate"],
            "halted_by": "workflow1.happy-1",
        },
    )
    called = []
    entrypoint = CampaignEntrypoint(
        repository_root=tmp_path,
        campaign_config_path=path,
        evidence_bundle=bundle,
        endpoints=_endpoints(),
        options=_options(
            artifact_root,
            sink_root,
            allow_mutation=True,
            mutation_acknowledgement=mutation_acknowledgement("evaluation-studio-v1"),
            allow_provider=True,
            paid_acknowledgement=provider_acknowledgement("evaluation-studio-v1"),
        ),
        database_factory=lambda: called.append("database"),
        client_factory=lambda: called.append("client"),
    )

    result = entrypoint.run()

    assert result.summary is not None
    assert result.summary.halted_by == "workflow1.happy-1"
    assert called == []


def test_cli_defaults_to_inert_preview(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    path, artifact_root, _ = _campaign(tmp_path)
    campaign = "evaluation-studio-v1"

    exit_code = main(
        [
            "--repository-root",
            str(repository),
            "--campaign-config",
            str(path),
            "--evidence-bundle",
            str(artifact_root / "bundle"),
            "--console-url",
            "http://127.0.0.1:8100",
            "--fault-control-url",
            "http://127.0.0.1:8101",
            "--deployment-url",
            f"{campaign}-grounded-researcher-v1=http://127.0.0.1:8101",
            "--deployment-url",
            f"{campaign}-batched-investigation-child-v1=http://127.0.0.1:8102",
            "--deployment-url",
            f"{campaign}-batched-investigation-parent-v1=http://127.0.0.1:8103",
            "--deployment-url",
            f"{campaign}-governed-remediation-v1=http://127.0.0.1:8104",
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "dry-run"
    assert not (artifact_root / "bundle").exists()


def test_local_controller_composition_uses_env_auth_and_exact_deployments(
    tmp_path: Path,
) -> None:
    path, artifact_root, sink_root = _campaign(tmp_path)
    campaign = CampaignConfig.model_validate_json(path.read_text(encoding="utf-8"))
    endpoints = _endpoints()
    store = EvidenceStore(artifact_root / "bundle")

    class Collection:
        name = f"evaluation-{tenant_slug(campaign.tenant_id)}"

    controller_requests: list[httpx.Request] = []

    def controller_health(request: httpx.Request) -> httpx.Response:
        controller_requests.append(request)
        return httpx.Response(200, json={"state": "ready"})

    client = httpx.Client(transport=httpx.MockTransport(controller_health))

    composition = compose_local_campaign_controllers(
        campaign=campaign,
        endpoints=endpoints,
        evidence_store=store,
        environment={
            "TEST_ZEROTH_API_KEY": "service-secret-value",
            "TEST_CONTROLLER_KEY": "controller-secret-value",
        },
        api_key_env="TEST_ZEROTH_API_KEY",
        controller_key_env="TEST_CONTROLLER_KEY",
        scenario_controller_url="http://127.0.0.1:8199",
        chroma_collection=Collection(),
        client=client,
    )

    assert composition.api_key == "service-secret-value"
    assert set(composition.runtime_gateway.deployments) == set(endpoints.deployment_base_urls)
    assert all(
        deployment.deployment_ref == reference
        for reference, deployment in composition.runtime_gateway.deployments.items()
    )
    assert composition.runtime_gateway.headers == {
        "X-API-Key": "service-secret-value",
        "X-Tenant-ID": campaign.tenant_id,
    }
    assert "service-secret-value" not in repr(composition)
    assert "controller-secret-value" not in repr(composition)
    assert composition.scenario_controller_url == "http://127.0.0.1:8199"
    assert composition.workflow1_controller is not None
    assert composition.workflow2_controller is not None
    assert composition.workflow3_controller is not None
    assert composition.workflow3_controller.sink.root == sink_root.resolve()
    composition.require_scenario_controller_ready(client)
    assert controller_requests[0].headers["X-Controller-Key"] == "controller-secret-value"


def test_embedded_controller_restart_uses_campaign_owned_supervisor(tmp_path: Path) -> None:
    path, artifact_root, _ = _campaign(tmp_path)
    campaign = CampaignConfig.model_validate_json(path.read_text(encoding="utf-8"))
    parent_ref = f"{campaign.campaign_id}-governed-remediation-v1"
    list_count = 0
    restarts: list[tuple[str, str]] = []

    class Collection:
        name = f"evaluation-{tenant_slug(campaign.tenant_id)}"

    class Supervisor:
        def restart(self, *, deployment_ref: str, service_url: str) -> None:
            restarts.append((deployment_ref, service_url))

    class Barriers:
        def wait_for(self, *, campaign_id: str, run_id: str, timeout_seconds: float):
            assert campaign_id == campaign.campaign_id
            assert run_id == "run-restart"
            assert timeout_seconds == 10
            return {
                "run_id": run_id,
                "operation_key": "operation-restart",
                "audit_id": f"{run_id}:audit:3",
                "audit_digest": "a" * 64,
                "audit_signature_sha256": hashlib.sha256(
                    b"hmac-sha256:signed"
                ).hexdigest(),
                "state": "waiting",
            }

        def mark_restarted(self, *, campaign_id: str, run_id: str) -> None:
            assert campaign_id == campaign.campaign_id
            assert run_id == "run-restart"

    def runtime(request: httpx.Request) -> httpx.Response:
        nonlocal list_count
        if request.url.path == "/admin/runs":
            list_count += 1
            return httpx.Response(
                200,
                json={
                    "runs": []
                    if list_count == 1
                    else [{"run_id": "run-restart", "deployment_ref": parent_ref}]
                },
            )
        if request.url.path == f"/v1/deployments/{parent_ref}/audits":
            return httpx.Response(
                200,
                json={
                    "records": [
                        {
                            "audit_id": "run-restart:audit:3",
                            "run_id": "run-restart",
                            "record_digest": "a" * 64,
                            "record_signature": "hmac-sha256:signed",
                        }
                    ]
                },
            )
        if request.url.path == "/v1/runs/run-restart/audit-verification":
            return httpx.Response(
                200, json={"verified": True, "signature_verified": True}
            )
        raise AssertionError(request.url)

    composition = compose_local_campaign_controllers(
        campaign=campaign,
        endpoints=_endpoints(),
        evidence_store=EvidenceStore(artifact_root / "bundle"),
        environment={
            "TEST_ZEROTH_API_KEY": "service-secret-value",
            "TEST_CONTROLLER_KEY": "controller-secret-value",
        },
        api_key_env="TEST_ZEROTH_API_KEY",
        controller_key_env="TEST_CONTROLLER_KEY",
        scenario_controller_url="http://127.0.0.1:8199",
        chroma_collection=Collection(),
        client=httpx.Client(transport=httpx.MockTransport(runtime)),
        supervisor=Supervisor(),
        receipt_barriers=Barriers(),
    )
    controller = TestClient(composition.scenario_app)
    headers = {"X-Controller-Key": "controller-secret-value"}
    prepared = controller.post(
        "/v1/scenarios/prepare",
        headers=headers,
        json={
            "scenario_id": "w3_restart_after_receipt",
            "workflow_id": parent_ref,
            "expected": {
                "run_status": "completed",
                "marker_count": 1,
                "reexecution_count": 0,
                "operation_status": "completed",
            },
        },
    )
    assert prepared.status_code == 200

    checkpoint = controller.post(
        (f"/v1/scenarios/{prepared.json()['fixture_id']}/checkpoints/restart_after_receipt_ready"),
        headers=headers,
    )

    assert checkpoint.status_code == 200
    assert restarts == [(parent_ref, "http://127.0.0.1:8104")]


@pytest.mark.parametrize("missing", ["api", "controller"])
def test_local_controller_composition_fails_closed_for_missing_env_auth(
    tmp_path: Path, missing: str
) -> None:
    path, artifact_root, _ = _campaign(tmp_path)
    campaign = CampaignConfig.model_validate_json(path.read_text(encoding="utf-8"))
    environment = {
        "TEST_ZEROTH_API_KEY": "service-secret-value",
        "TEST_CONTROLLER_KEY": "controller-secret-value",
    }
    environment.pop("TEST_ZEROTH_API_KEY" if missing == "api" else "TEST_CONTROLLER_KEY")

    with pytest.raises(ValueError, match="environment variable"):
        compose_local_campaign_controllers(
            campaign=campaign,
            endpoints=_endpoints(),
            evidence_store=EvidenceStore(artifact_root / "bundle"),
            environment=environment,
            api_key_env="TEST_ZEROTH_API_KEY",
            controller_key_env="TEST_CONTROLLER_KEY",
            scenario_controller_url="http://127.0.0.1:8199",
            chroma_collection=object(),
        )


def test_cli_exposes_connector_and_auth_env_names_without_accepting_secret_values() -> None:
    parser = entrypoint_module.build_parser()
    option_strings = {option for action in parser._actions for option in action.option_strings}

    assert "--chroma-connector-ref" in option_strings
    assert "--api-key-env" in option_strings
    assert "--controller-key-env" in option_strings
    assert "--scenario-controller-url" in option_strings
    assert "--chroma-url" in option_strings
    assert "--chroma-collection" in option_strings
    assert "--api-key" not in option_strings
    assert "--controller-key" not in option_strings
    assert "--stop-after-workflow1" in option_strings


def test_authorized_workflow1_fallback_blocks_every_later_gate_and_resumes_inertly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, artifact_root, sink_root = _campaign(tmp_path)
    bundle = artifact_root / "bundle"
    _seed_control_bundle(bundle)
    observed_workflows: list[str] = []

    class Backend:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def execute(self, action, recorder) -> StepResult:
            observed_workflows.append(action.workflow)
            evidence = recorder.record_ui_action(
                action="workflow1-fallback-test",
                outcome="pass",
                metadata={"scenario": action.scenario, "workflow": action.workflow},
            )
            return StepResult(
                tuple(
                    CriterionResult(criterion_id, "pass", (evidence,))
                    for criterion_id in action.criterion_ids
                )
            )

    class Supervisor:
        def restart(self, *, deployment_ref: str, service_url: str) -> None:
            del deployment_ref, service_url

        def stop_all(self) -> None:
            return None

    class ForbiddenCrossCutting:
        def execute(self, **kwargs: object) -> StepResult:
            del kwargs
            raise AssertionError("fallback must not run cross-cutting producers")

    class ForbiddenFinalizer:
        def finalize(self, **kwargs: object) -> None:
            del kwargs
            raise AssertionError("fallback must never finalize as a complete campaign")

    monkeypatch.setattr(entrypoint_module, "HttpCampaignExecutionBackend", Backend)
    options = _options(
        artifact_root,
        sink_root,
        allow_mutation=True,
        mutation_acknowledgement=mutation_acknowledgement("evaluation-studio-v1"),
        allow_provider=True,
        paid_acknowledgement=provider_acknowledgement("evaluation-studio-v1"),
    )
    first = CampaignEntrypoint(
        repository_root=tmp_path,
        campaign_config_path=path,
        evidence_bundle=bundle,
        endpoints=_endpoints(),
        options=options,
        database_factory=lambda: object(),
        client_factory=lambda: httpx.Client(),
        publisher_factory=lambda database: object(),
        supervisor_factory=lambda store: Supervisor(),
        workflow1_scenario_controller=object(),
        cross_cutting_gate_executor=ForbiddenCrossCutting(),
        evidence_finalizer=ForbiddenFinalizer(),
        stop_after_workflow1=True,
    ).run()

    assert first.mode == "workflow1-fallback"
    assert first.summary is not None
    assert first.summary.completed is False
    assert first.summary.halted_by == "authorized.workflow1-fallback"
    assert first.summary.check_ran is False
    assert set(observed_workflows) == {"workflow1"}
    ledger = CampaignLedger(EvidenceStore(bundle), original_acceptance_criteria())
    statuses = {item.criterion_id: item.status for item in ledger.criteria}
    allowed_pass = {
        criterion_id
        for criterion_id in statuses
        if criterion_id.startswith("control.")
        or criterion_id.startswith("workflow1.")
        or criterion_id == "audit.probe-events-instrumented"
    }
    assert all(statuses[item] == "pass" for item in allowed_pass)
    assert all(
        status == "blocked"
        for criterion_id, status in statuses.items()
        if criterion_id not in allowed_pass
    )
    blocked = [item for item in ledger.criteria if item.criterion_id not in allowed_pass]
    assert {item.note for item in blocked} == {entrypoint_module.WORKFLOW1_FALLBACK_REASON}
    assert len({item.evidence for item in blocked}) == 1
    assert blocked[0].evidence[0].startswith("events.ndjson#")
    events = (bundle / "events.ndjson").read_text()
    assert "campaign.workflow1-fallback.authorized" in events
    assert "campaign.workflow1-fallback.completed" in events
    assert "shared-project-upper-bound" in events
    assert "campaign.stage.completed" not in events
    assert "campaign.completed" not in events

    factory_calls: list[str] = []
    resumed = CampaignEntrypoint(
        repository_root=tmp_path,
        campaign_config_path=path,
        evidence_bundle=bundle,
        endpoints=_endpoints(),
        options=options,
        database_factory=lambda: factory_calls.append("database"),
        client_factory=lambda: factory_calls.append("client"),
        stop_after_workflow1=True,
    ).run()
    assert resumed.mode == "workflow1-fallback"
    assert resumed.summary is not None and resumed.summary.completed is False
    assert factory_calls == []
    assert observed_workflows and set(observed_workflows) == {"workflow1"}


def test_live_cli_passes_only_named_auth_environment_and_controller_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    path, artifact_root, _ = _campaign(tmp_path)
    campaign = "evaluation-studio-v1"
    captured: dict[str, object] = {}

    class Entrypoint:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def run(self, *, dry_run: bool = False) -> object:
            assert dry_run is False
            return entrypoint_module.CampaignEntrypointResult(
                mode="live",
                campaign_id=campaign,
                evidence_bundle=artifact_root / "bundle",
            )

    monkeypatch.setattr(entrypoint_module, "CampaignEntrypoint", Entrypoint)
    monkeypatch.setenv("TEST_ZEROTH_API_KEY", "service-secret-value")
    monkeypatch.setenv("TEST_CONTROLLER_KEY", "controller-secret-value")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-be-copied")

    exit_code = main(
        [
            "--repository-root",
            str(repository),
            "--campaign-config",
            str(path),
            "--evidence-bundle",
            str(artifact_root / "bundle"),
            "--console-url",
            "http://127.0.0.1:8100",
            "--fault-control-url",
            "http://127.0.0.1:8101",
            "--deployment-url",
            f"{campaign}-grounded-researcher-v1=http://127.0.0.1:8101",
            "--deployment-url",
            f"{campaign}-batched-investigation-child-v1=http://127.0.0.1:8102",
            "--deployment-url",
            f"{campaign}-batched-investigation-parent-v1=http://127.0.0.1:8103",
            "--deployment-url",
            f"{campaign}-governed-remediation-v1=http://127.0.0.1:8104",
            "--chroma-connector-ref",
            "campaign-chroma",
            "--api-key-env",
            "TEST_ZEROTH_API_KEY",
            "--controller-key-env",
            "TEST_CONTROLLER_KEY",
            "--scenario-controller-url",
            "http://127.0.0.1:8199",
            "--chroma-url",
            "http://127.0.0.1:8121",
            "--chroma-collection",
            "evaluation-tenant-collection",
            "--stop-after-workflow1",
            "--execute",
        ]
    )

    assert exit_code == 0
    assert captured["compose_local_controllers"] is True
    assert captured["chroma_connector_ref"] == "campaign-chroma"
    assert captured["api_key_env"] == "TEST_ZEROTH_API_KEY"
    assert captured["controller_key_env"] == "TEST_CONTROLLER_KEY"
    assert captured["scenario_controller_url"] == "http://127.0.0.1:8199"
    assert captured["chroma_url"] == "http://127.0.0.1:8121"
    assert captured["chroma_collection_name"] == "evaluation-tenant-collection"
    assert captured["stop_after_workflow1"] is True
    assert captured["controller_environment"] == {
        "TEST_ZEROTH_API_KEY": "service-secret-value",
        "TEST_CONTROLLER_KEY": "controller-secret-value",
    }
    output = capsys.readouterr().out
    assert "service-secret-value" not in output
    assert "controller-secret-value" not in output


@pytest.mark.parametrize(
    "cross_cutting_mode", ["default", "complete", "incomplete", "stage_resume"]
)
def test_controllers_are_wired_and_only_exact_cross_cutting_results_can_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cross_cutting_mode: str,
) -> None:
    path, artifact_root, sink_root = _campaign(tmp_path)
    _seed_control_bundle(artifact_root / "bundle")
    controller = object()
    workflow2_controller = object()
    workflow3_controller = object()
    captured = {}

    class Backend:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def execute(self, action, recorder) -> StepResult:
            evidence = recorder.record_ui_action(
                action="fake-action",
                outcome="pass",
                metadata={"scenario": action.scenario},
            )
            return StepResult(
                tuple(CriterionResult(item, "pass", (evidence,)) for item in action.criterion_ids)
            )

    class Supervisor:
        def restart(self, *, deployment_ref: str, service_url: str) -> None:
            del deployment_ref, service_url

        def stop_all(self) -> None:
            return None

    class CrossCutting:
        def execute(self, *, phase: Phase, criterion_ids, recorder) -> StepResult:
            evidence = recorder.record_ui_action(
                action="cross-cutting-gate",
                outcome="pass",
                metadata={"phase": phase.name},
            )
            return StepResult(
                tuple(
                    CriterionResult(criterion_id, "pass", (evidence,))
                    for criterion_id in criterion_ids
                )
            )

    class IncompleteCrossCutting(CrossCutting):
        def execute(self, *, phase: Phase, criterion_ids, recorder) -> StepResult:
            result = super().execute(phase=phase, criterion_ids=criterion_ids, recorder=recorder)
            return StepResult(result.criteria[:-1])

    class Finalizer:
        def finalize(self, *, store: EvidenceStore, ledger: CampaignLedger) -> None:
            event_id = store.append_event(
                "campaign.finalization.ready",
                {"derived_artifact_count": 3},
            )
            evidence = (f"events.ndjson#{event_id}",)
            for criterion_id in (
                "evidence.acceptance",
                "evidence.report",
                "evidence.sha256-checksums",
            ):
                ledger.record(criterion_id, "pass", evidence=evidence)
            store.finalize_bundle(
                acceptance=ledger.resolved_criteria(),
                report_markdown="# Complete evaluation\n",
            )

    monkeypatch.setattr(entrypoint_module, "HttpCampaignExecutionBackend", Backend)
    cross_cutting = (
        CrossCutting()
        if cross_cutting_mode in {"complete", "stage_resume"}
        else IncompleteCrossCutting()
        if cross_cutting_mode == "incomplete"
        else None
    )
    live_options = _options(
        artifact_root,
        sink_root,
        allow_mutation=True,
        mutation_acknowledgement=mutation_acknowledgement("evaluation-studio-v1"),
        allow_provider=True,
        paid_acknowledgement=provider_acknowledgement("evaluation-studio-v1"),
    )
    campaign_entrypoint = CampaignEntrypoint(
        repository_root=tmp_path,
        campaign_config_path=path,
        evidence_bundle=artifact_root / "bundle",
        endpoints=_endpoints(),
        options=live_options,
        database_factory=lambda: object(),
        client_factory=lambda: httpx.Client(),
        publisher_factory=lambda database: object(),
        supervisor_factory=lambda store: Supervisor(),
        workflow1_scenario_controller=controller,
        workflow2_scenario_controller=workflow2_controller,
        workflow3_scenario_controller=workflow3_controller,
        cross_cutting_gate_executor=cross_cutting,
        evidence_finalizer=Finalizer() if cross_cutting_mode == "complete" else None,
    )

    if cross_cutting_mode == "incomplete":
        with pytest.raises(ValueError, match="must return each registered criterion exactly once"):
            campaign_entrypoint.run()
        assert "campaign.completed" not in (artifact_root / "bundle" / "events.ndjson").read_text()
        return

    result = campaign_entrypoint.run()

    assert result.summary is not None
    assert result.summary.completed is (cross_cutting_mode == "complete")
    assert (
        result.summary.halted_by
        == {
            "complete": None,
            "default": "check.after-workflow-gates",
            "stage_resume": "finalization.required",
        }[cross_cutting_mode]
    )
    assert (artifact_root / "bundle" / "SHA256SUMS").exists() is (cross_cutting_mode == "complete")
    assert captured["workflow1_scenario_controller"] is controller
    assert captured["workflow2_scenario_controller"] is workflow2_controller
    assert captured["workflow3_scenario_controller"] is workflow3_controller
    events = (artifact_root / "bundle" / "events.ndjson").read_text()
    assert '"workflow2":"implemented_controller_required"' in events.replace(" ", "")
    assert '"workflow3":"implemented_transport_controller_required"' in events.replace(" ", "")
    if cross_cutting_mode == "complete":
        event_bytes = (artifact_root / "bundle" / "events.ndjson").read_bytes()
        assert b"campaign.completed" not in event_bytes
        assert b"campaign.stage.completed" in event_bytes
        called = []
        resumed = CampaignEntrypoint(
            repository_root=tmp_path,
            campaign_config_path=path,
            evidence_bundle=artifact_root / "bundle",
            endpoints=_endpoints(),
            options=live_options,
            database_factory=lambda: called.append("database"),
            client_factory=lambda: called.append("client"),
        ).run()
        assert resumed.summary is not None and resumed.summary.completed
        assert called == []
        assert (artifact_root / "bundle" / "events.ndjson").read_bytes() == event_bytes
    elif cross_cutting_mode == "stage_resume":
        called = []
        resumed = CampaignEntrypoint(
            repository_root=tmp_path,
            campaign_config_path=path,
            evidence_bundle=artifact_root / "bundle",
            endpoints=_endpoints(),
            options=live_options,
            database_factory=lambda: called.append("database"),
            client_factory=lambda: called.append("client"),
            evidence_finalizer=Finalizer(),
        ).run()
        assert resumed.summary is not None and resumed.summary.completed
        assert called == []
        assert (artifact_root / "bundle" / "SHA256SUMS").is_file()


def test_fresh_bundle_cannot_cross_runtime_boundary_without_manifest_and_control_ledger(
    tmp_path: Path,
) -> None:
    path, artifact_root, sink_root = _campaign(tmp_path)
    fresh = artifact_root / "fresh-bundle"
    fresh.mkdir(parents=True)
    called = []
    entrypoint = CampaignEntrypoint(
        repository_root=tmp_path,
        campaign_config_path=path,
        evidence_bundle=fresh,
        endpoints=_endpoints(),
        options=_options(
            artifact_root,
            sink_root,
            allow_mutation=True,
            mutation_acknowledgement=mutation_acknowledgement("evaluation-studio-v1"),
            allow_provider=True,
            paid_acknowledgement=provider_acknowledgement("evaluation-studio-v1"),
        ),
        database_factory=lambda: called.append("database"),
        client_factory=lambda: called.append("client"),
    )

    with pytest.raises(RuntimeError, match="control manifest"):
        entrypoint.run()

    assert called == []
    assert not (fresh / "events.ndjson").exists()


def test_corrupt_database_snapshot_fails_before_runtime_factories(tmp_path: Path) -> None:
    path, artifact_root, sink_root = _campaign(tmp_path)
    bundle = artifact_root / "bundle"
    _seed_control_bundle(bundle)
    (bundle / "database-snapshots" / "service-pretest.sqlite3").write_bytes(
        b"not-a-sqlite-database"
    )
    called = []
    entrypoint = CampaignEntrypoint(
        repository_root=tmp_path,
        campaign_config_path=path,
        evidence_bundle=bundle,
        endpoints=_endpoints(),
        options=_options(
            artifact_root,
            sink_root,
            allow_mutation=True,
            mutation_acknowledgement=mutation_acknowledgement("evaluation-studio-v1"),
            allow_provider=True,
            paid_acknowledgement=provider_acknowledgement("evaluation-studio-v1"),
        ),
        database_factory=lambda: called.append("database"),
        client_factory=lambda: called.append("client"),
    )

    with pytest.raises(RuntimeError, match="valid SQLite"):
        entrypoint.run()

    assert called == []
