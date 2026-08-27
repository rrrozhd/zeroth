from __future__ import annotations

import json
import sqlite3
import subprocess
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from release.live_evaluation.config import CampaignConfig
from release.live_evaluation.control_adapters import BatchEmbeddingResult
from release.live_evaluation.control_entrypoint import (
    ControlPlaneClients,
    ControlPlaneEntrypoint,
    ControlPlaneOptions,
    main,
    paid_control_acknowledgement,
)
from release.live_evaluation.runner import CommandResult, EvaluationReport
from zeroth.econ.plane.enforcement.models import CostReservation, TenantBudget
from zeroth.econ.plane.instrumentation.models import ExecutionEvent
from zeroth.integrations.memory.tenant_scoped import tenant_slug
from sqlalchemy import create_engine


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ("git", *args), cwd=repository, check=True, capture_output=True, text=True
    ).stdout.strip()


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "evaluation@example.invalid")
    _git(root, "config", "user.name", "Evaluation Test")
    (root / "tracked.txt").write_text("baseline\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    return root


def _campaign(tmp_path: Path) -> CampaignConfig:
    external = tmp_path / "external"
    return CampaignConfig.model_validate(
        {
            "schema_version": 1,
            "campaign_id": "evaluation-control-root",
            "tenant_id": "evaluation-control-root",
            "provider": "openai",
            "model": "openai/gpt-4o-mini",
            "embedding_model": "openai/text-embedding-3-small",
            "vector_backend": "chroma",
            "campaign_budget_usd": "10.00",
            "per_run_cap_usd": "0.25",
            "provider_secret_ref": "llm.openai",
            "artifact_root": str(external),
            "action_sink_root": str(external / "action-sink"),
        }
    )


def _options(tmp_path: Path, repository: Path) -> ControlPlaneOptions:
    database = tmp_path / "external" / "service.sqlite3"
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS witness (value TEXT)")
    econ = tmp_path / "external" / "econ.sqlite3"
    engine = create_engine(f"sqlite+pysqlite:///{econ}")
    for table in (TenantBudget.__table__, ExecutionEvent.__table__, CostReservation.__table__):
        table.create(engine, checkfirst=True)
    engine.dispose()
    return ControlPlaneOptions(
        repository_root=repository,
        evidence_root=tmp_path / "external" / "evidence",
        econ_database=econ,
        sqlite_sources={"service": database, "economics": econ},
        runtime_versions={"python": "test", "zeroth": "test"},
        browser_versions={"chromium": "test"},
        container_versions={"chroma": "chromadb/chroma:1.5.6"},
        signing_reference="evaluation.control.signing",
        chroma_container_name="zeroth-evaluation-chroma",
        chroma_image="chromadb/chroma:1.5.6",
        chroma_host="127.0.0.1",
        chroma_port=8121,
        provider_workflow_id="bootstrap-provider",
        chroma_connector_ref="chroma-evaluation",
    )


def _baseline_runner(calls: list[str]):
    def run(commands, *, artifact_root, evidence_store):
        calls.append("baseline")
        results = []
        for index, command in enumerate(commands, start=1):
            path = evidence_store.record_command(
                sequence=index,
                name=command.name,
                argv=command.argv,
                working_directory=artifact_root,
                exit_code=0,
                stdout="passed",
                stderr="",
            )
            results.append(CommandResult(command.name, 0, artifact_root, "passed", "", path))
        return EvaluationReport(tuple(results))

    return run


def test_default_run_is_inert_after_capture_and_local_baseline(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    calls: list[str] = []
    entrypoint = ControlPlaneEntrypoint(
        campaign=_campaign(tmp_path),
        options=_options(tmp_path, repository),
        baseline_runner=_baseline_runner(calls),
    )

    result = entrypoint.run()

    assert result.phase == "awaiting_paid_acknowledgement"
    assert calls == ["baseline"]
    assert (result.bundle_root / "manifest.json").is_file()
    manifest = json.loads((result.bundle_root / "manifest.json").read_text())
    assert len(manifest["pretest_sqlite_snapshots"]) == 2
    assert not [event for event in result.events if str(event["type"]).startswith("control.probe.")]


def test_attested_snapshot_bundle_resumes_without_raw_sqlite(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    calls: list[str] = []
    options = replace(_options(tmp_path, repository), retain_raw_snapshots=False)
    entrypoint = ControlPlaneEntrypoint(
        campaign=_campaign(tmp_path),
        options=options,
        baseline_runner=_baseline_runner(calls),
    )

    prepared = entrypoint.run()
    manifest = json.loads((prepared.bundle_root / "manifest.json").read_text())
    assert manifest["pretest_sqlite_snapshot_attestations"].endswith(".json")
    assert not list((prepared.bundle_root / "database-snapshots").glob("*.sqlite3"))

    resumed = ControlPlaneEntrypoint(
        campaign=_campaign(tmp_path),
        options=replace(options, bundle_root=prepared.bundle_root),
        baseline_runner=_baseline_runner(calls),
    ).run()

    assert resumed.bundle_root == prepared.bundle_root
    assert resumed.phase == "awaiting_paid_acknowledgement"
    assert calls == ["baseline"]


@dataclass
class _NoopEmbeddingExecutor:
    calls: int = 0

    def embed_once(self, **kwargs):
        self.calls += 1
        raise AssertionError("the composition test should replace the concrete seeder")


class _Collection:
    def __init__(self, tenant_id: str) -> None:
        self.name = f"evaluation-{tenant_slug(tenant_id)}"
        self.rows: dict[str, tuple[str, list[float], dict[str, str]]] = {}

    def get(self, include):
        ids = list(self.rows)
        return {
            "ids": ids,
            "documents": [self.rows[item][0] for item in ids],
            "embeddings": [self.rows[item][1] for item in ids],
            "metadatas": [self.rows[item][2] for item in ids],
        }

    def delete(self, ids):
        for item in ids:
            self.rows.pop(item, None)

    def upsert(self, *, ids, documents, embeddings, metadatas):
        self.rows = {
            item: (document, embedding, metadata)
            for item, document, embedding, metadata in zip(
                ids, documents, embeddings, metadatas, strict=True
            )
        }


@dataclass
class _EmbeddingExecutor:
    calls: int = 0

    def embed_once(self, *, model, inputs, operation_id, run_id):
        self.calls += 1
        return BatchEmbeddingResult(
            model=model,
            vectors=tuple([float(index), 1.0] for index, _ in enumerate(inputs, start=1)),
            request_count=1,
            operation_id=operation_id,
            run_id=run_id,
            provider_request_id="provider-seed-request",
            cost_event_id="cost-seed-event",
            audit_event_id="audit-seed-event",
            cleanup_state="committed",
            measured_cost_usd=Decimal("0.00001"),
        )


def _paid_clients(campaign: CampaignConfig, calls: list[str]) -> ControlPlaneClients:
    def service_handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/v1/audit-readiness":
            return httpx.Response(
                200,
                json={"ready": True, "state": "signed", "signer_available": True},
            )
        body = json.loads(request.content)
        operation = body["operation_id"]
        if request.url.path == "/v1/evaluation/control/chroma-corpus/seed":
            return httpx.Response(
                200,
                json={
                    "campaign_id": campaign.campaign_id,
                    "operation_id": operation,
                    "run_id": body["run_id"],
                    "request_count": 1,
                    "cleanup_status": "complete",
                    "audit_event_id": "audit-http-corpus-seed",
                    "cost_event_id": "cost-http-corpus-seed",
                    "provider_request_id": "request-http-corpus-seed",
                    "documents": [
                        {
                            "document_id": item["document_id"],
                            "tenant_id": campaign.tenant_id,
                            "sha256": item["sha256"],
                        }
                        for item in body["documents"]
                    ],
                },
            )
        if request.url.path.endswith("/verify-provider"):
            return httpx.Response(
                200,
                json={
                    "verified": True,
                    "campaign_id": campaign.campaign_id,
                    "operation_id": operation,
                    "probes": [
                        {
                            "ok": True,
                            "operation_id": operation,
                            "cleanup_status": "complete",
                            "cost_measurement": "estimated",
                            "estimated_cost_usd": "0.00002",
                            "cost_event_id": "cost-provider-probe",
                            "audit_event_id": "audit-provider-probe",
                            "provider_request_id": "request-provider-probe",
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "ok": True,
                "campaign_id": campaign.campaign_id,
                "operation_id": operation,
                "cleanup_status": "complete",
                "cost_measurement": "estimated",
                "estimated_cost_usd": "0.00001",
                "cost_event_id": "cost-chroma-probe",
                "audit_event_id": "audit-chroma-probe",
                "provider_request_id": "request-chroma-probe",
            },
        )

    service = httpx.Client(
        base_url="http://127.0.0.1:7000",
        headers={"Authorization": "Bearer client-owned-not-persisted"},
        transport=httpx.MockTransport(service_handler),
    )
    chroma = httpx.Client(
        base_url="http://127.0.0.1:8121",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"version": "1.5.6"})
        ),
    )
    inspect_payload = [
        {
            "Id": "chroma-instance",
            "Name": "/zeroth-evaluation-chroma",
            "Config": {"Image": "chromadb/chroma:1.5.6"},
            "State": {"Running": True, "Health": {"Status": "healthy"}},
            "NetworkSettings": {
                "Ports": {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8121"}]}
            },
        }
    ]
    return ControlPlaneClients(
        service_client=service,
        chroma_client=chroma,
        chroma_collection=_Collection(campaign.tenant_id),
        embedding_executor=_EmbeddingExecutor(),
        docker_command_runner=lambda argv: (0, json.dumps(inspect_payload), ""),
    )


def test_wrong_paid_acknowledgement_fails_before_clients_are_touched(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    entrypoint = ControlPlaneEntrypoint(
        campaign=_campaign(tmp_path),
        options=_options(tmp_path, repository),
        baseline_runner=_baseline_runner([]),
    )
    marker: list[httpx.Request] = []
    client = httpx.Client(
        base_url="http://127.0.0.1:7000",
        transport=httpx.MockTransport(lambda request: marker.append(request)),
    )

    with pytest.raises(ValueError, match="exact paid control acknowledgement"):
        entrypoint.run(
            paid_acknowledgement="wrong",
            clients=ControlPlaneClients(
                service_client=client,
                chroma_client=client,
                chroma_collection=object(),
                embedding_executor=_NoopEmbeddingExecutor(),
            ),
        )

    assert marker == []


def test_external_roots_and_loopback_clients_are_required_before_execution(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    campaign = _campaign(tmp_path)
    options = _options(tmp_path, repository)
    with pytest.raises(ValueError, match="outside the repository"):
        ControlPlaneEntrypoint(
            campaign=campaign,
            options=ControlPlaneOptions(
                **{**options.__dict__, "evidence_root": repository / "evidence"}
            ),
            baseline_runner=_baseline_runner([]),
        )

    entrypoint = ControlPlaneEntrypoint(
        campaign=campaign,
        options=options,
        baseline_runner=_baseline_runner([]),
    )
    remote = httpx.Client(base_url="https://example.com")
    with pytest.raises(ValueError, match="loopback"):
        entrypoint.run(
            paid_acknowledgement=paid_control_acknowledgement(campaign.campaign_id),
            clients=ControlPlaneClients(
                service_client=remote,
                chroma_client=remote,
                chroma_collection=object(),
                embedding_executor=_NoopEmbeddingExecutor(),
            ),
        )


def test_resume_does_not_repeat_baseline_or_completed_paid_probes(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    baseline_calls: list[str] = []
    first = ControlPlaneEntrypoint(
        campaign=_campaign(tmp_path),
        options=_options(tmp_path, repository),
        baseline_runner=_baseline_runner(baseline_calls),
    ).run()

    resumed_options = ControlPlaneOptions(
        **{**_options(tmp_path, repository).__dict__, "bundle_root": first.bundle_root}
    )
    resumed = ControlPlaneEntrypoint(
        campaign=_campaign(tmp_path),
        options=resumed_options,
        baseline_runner=_baseline_runner(baseline_calls),
    ).run()

    assert resumed.bundle_root == first.bundle_root
    assert resumed.phase == "awaiting_paid_acknowledgement"
    assert baseline_calls == ["baseline"]


def test_exact_ack_runs_concrete_adapters_once_and_completed_resume_is_inert(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    campaign = _campaign(tmp_path)
    options = _options(tmp_path, repository)
    calls: list[str] = []
    clients = _paid_clients(campaign, calls)
    first = ControlPlaneEntrypoint(
        campaign=campaign,
        options=options,
        baseline_runner=_baseline_runner([]),
    ).run(
        paid_acknowledgement=paid_control_acknowledgement(campaign.campaign_id),
        clients=clients,
    )

    assert first.phase == "control_complete"
    assert [item.kind for item in first.newly_executed_probes] == ["provider", "chroma"]
    assert calls.count("/v1/audit-readiness") == 1
    assert len([path for path in calls if path.endswith("/verify-provider")]) == 1
    assert calls.count("/v1/connectors/chroma-evaluation/test") == 1
    evidence_text = (first.bundle_root / "events.ndjson").read_text()
    assert "client-owned-not-persisted" not in evidence_text
    assert "Authorization" not in evidence_text

    before = list(calls)
    resumed = ControlPlaneEntrypoint(
        campaign=campaign,
        options=ControlPlaneOptions(**{**options.__dict__, "bundle_root": first.bundle_root}),
        baseline_runner=_baseline_runner([]),
    ).run(
        paid_acknowledgement=paid_control_acknowledgement(campaign.campaign_id),
        clients=clients,
    )
    assert resumed.phase == "control_complete"
    assert resumed.newly_executed_probes == ()
    assert calls == before


def test_interrupted_authorization_is_ambiguous_and_never_reexecuted(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    campaign = _campaign(tmp_path)
    options = _options(tmp_path, repository)
    calls: list[str] = []
    clients = _paid_clients(campaign, calls)

    def fail_after_readiness(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/v1/audit-readiness":
            return httpx.Response(
                200,
                json={"ready": True, "state": "signed", "signer_available": True},
            )
        raise httpx.ConnectError("ambiguous provider outcome", request=request)

    failing = replace(
        clients,
        service_client=httpx.Client(
            base_url="http://127.0.0.1:7000",
            transport=httpx.MockTransport(fail_after_readiness),
        ),
    )
    entrypoint = ControlPlaneEntrypoint(
        campaign=campaign,
        options=options,
        baseline_runner=_baseline_runner([]),
    )
    with pytest.raises(RuntimeError, match="provider paid probe service is unavailable"):
        entrypoint.run(
            paid_acknowledgement=paid_control_acknowledgement(campaign.campaign_id),
            clients=failing,
        )
    bundle = next(options.evidence_root.iterdir())
    before = list(calls)

    resumed = ControlPlaneEntrypoint(
        campaign=campaign,
        options=replace(options, bundle_root=bundle),
        baseline_runner=_baseline_runner([]),
    ).run(
        paid_acknowledgement=paid_control_acknowledgement(campaign.campaign_id),
        clients=clients,
    )

    assert resumed.phase == "control_ambiguous"
    assert calls == before
    assert (
        len([event for event in resumed.events if event["type"] == "control.probe.authorized"]) == 1
    )


def test_acknowledgement_is_campaign_specific() -> None:
    assert paid_control_acknowledgement("evaluation-a") != paid_control_acknowledgement(
        "evaluation-b"
    )


def test_cli_is_capture_only_and_reports_resumable_bundle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repository(tmp_path)
    campaign = _campaign(tmp_path)
    options = _options(tmp_path, repository)
    config = tmp_path / "campaign.json"
    config.write_text(campaign.model_dump_json())
    calls: list[str] = []

    exit_code = main(
        (
            "--campaign-config",
            str(config),
            "--repository-root",
            str(repository),
            "--evidence-root",
            str(options.evidence_root),
            "--econ-database",
            str(options.econ_database),
            "--sqlite",
            f"service={options.sqlite_sources['service']}",
            "--runtime-version",
            "python=test",
            "--browser-version",
            "chromium=test",
            "--container-version",
            "chroma=chromadb/chroma:1.5.6",
        ),
        baseline_runner=_baseline_runner(calls),
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["phase"] == "awaiting_paid_acknowledgement"
    assert Path(output["bundle_root"]).is_dir()
    assert calls == ["baseline"]


def test_cli_paid_mode_uses_environment_auth_and_resumes_prepared_bundle(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    campaign = _campaign(tmp_path)
    options = _options(tmp_path, repository)
    config = tmp_path / "campaign.json"
    config.write_text(campaign.model_dump_json())
    common = (
        "--campaign-config",
        str(config),
        "--repository-root",
        str(repository),
        "--evidence-root",
        str(options.evidence_root),
        "--econ-database",
        str(options.econ_database),
        "--sqlite",
        f"service={options.sqlite_sources['service']}",
        "--runtime-version",
        "python=test",
        "--browser-version",
        "chromium=test",
        "--container-version",
        "chroma=chromadb/chroma:1.5.6",
    )
    baseline_calls: list[str] = []
    assert main(common, baseline_runner=_baseline_runner(baseline_calls)) == 0
    bundle = Path(json.loads(capsys.readouterr().out)["bundle_root"])

    http_calls: list[str] = []
    clients = _paid_clients(campaign, http_calls)

    observed_service_headers: dict[str, str] = {}

    def client_factory(*, base_url, **kwargs):
        if str(base_url).endswith("8121"):
            return clients.chroma_client
        observed_service_headers.update(kwargs["headers"])
        return clients.service_client

    inspect_payload = [
        {
            "Id": "chroma-instance",
            "Name": "/zeroth-evaluation-chroma",
            "Config": {"Image": "chromadb/chroma:1.5.6"},
            "State": {"Running": True, "Health": {"Status": "healthy"}},
            "NetworkSettings": {
                "Ports": {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8121"}]}
            },
        }
    ]
    monkeypatch.setenv("CONTROL_TEST_AUTH", "client-owned-auth-value")
    paid = common + (
        "--resume-bundle",
        str(bundle),
        "--execute-paid-control",
        "--paid-ack",
        paid_control_acknowledgement(campaign.campaign_id),
        "--service-auth-env",
        "CONTROL_TEST_AUTH",
        "--provider-workflow-id",
        "bootstrap-provider",
        "--chroma-connector-ref",
        "chroma-evaluation",
    )
    assert (
        main(
            paid,
            baseline_runner=_baseline_runner(baseline_calls),
            http_client_factory=client_factory,
            docker_command_runner=lambda argv: (0, json.dumps(inspect_payload), ""),
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["phase"] == "control_complete"
    assert http_calls.count("/v1/evaluation/control/chroma-corpus/seed") == 1
    assert len([path for path in http_calls if path.endswith("/verify-provider")]) == 1
    assert http_calls.count("/v1/connectors/chroma-evaluation/test") == 1
    assert observed_service_headers == {"X-API-Key": "client-owned-auth-value"}
    evidence = (bundle / "events.ndjson").read_text()
    assert "client-owned-auth-value" not in evidence
    assert "Authorization" not in evidence

    before = list(http_calls)
    assert (
        main(
            paid,
            baseline_runner=_baseline_runner(baseline_calls),
            http_client_factory=client_factory,
            docker_command_runner=lambda argv: (0, json.dumps(inspect_payload), ""),
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["phase"] == "control_complete"
    assert http_calls == before
