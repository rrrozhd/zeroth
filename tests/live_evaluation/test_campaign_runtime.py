from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from release.live_evaluation.campaign_execution import (
    CampaignExecutionSettings,
    build_campaign_execution,
)
from release.live_evaluation.campaign_http import PublishedGraph
from release.live_evaluation.campaign_runtime import (
    LocalDeploymentSupervisor,
    RepositoryTenantGraphPublisher,
    load_live_execution_cli,
    load_live_execution_options,
)
from release.live_evaluation.evidence import EvidenceStore


def _execution():
    return build_campaign_execution(
        CampaignExecutionSettings(
            campaign_id="evaluation-studio-v1",
            tenant_id="evaluation-studio-v1",
            model="openai/gpt-4o-mini",
            embedding_model="openai/text-embedding-3-small",
            chroma_connector_ref="eval_chroma_v1",
        )
    )


async def test_repository_publisher_registers_contracts_and_publishes_exact_ids_idempotently(
    sqlite_db,
) -> None:
    execution = _execution()
    publisher = RepositoryTenantGraphPublisher(sqlite_db)
    graphs = (execution.graphs.workflow1,)

    first = await publisher.publish_async(
        graphs=graphs,
        contracts=execution.contracts,
        tenant_id=execution.settings.tenant_id,
        workspace_id=None,
    )
    second = await publisher.publish_async(
        graphs=graphs,
        contracts=execution.contracts,
        tenant_id=execution.settings.tenant_id,
        workspace_id=None,
    )

    assert first == second == (PublishedGraph(graphs[0].graph_id, 1),)
    stored = await publisher.graph_repository.get(
        graphs[0].graph_id,
        1,
        tenant_id=execution.settings.tenant_id,
        workspace_id=None,
    )
    assert stored is not None
    assert stored.status.value == "published"
    assert stored.graph_id == graphs[0].graph_id
    assert (
        await publisher.contract_registry(execution.settings.tenant_id, None).latest_version(
            "evaluation-studio-v1.workflow1.query"
        )
        == 1
    )


async def test_repository_publisher_refuses_same_id_version_with_different_graph(
    sqlite_db,
) -> None:
    execution = _execution()
    publisher = RepositoryTenantGraphPublisher(sqlite_db)
    graph = execution.graphs.workflow1
    await publisher.publish_async(
        graphs=(graph,),
        contracts=execution.contracts,
        tenant_id=execution.settings.tenant_id,
        workspace_id=None,
    )

    changed = graph.model_copy(update={"name": "different"})
    with pytest.raises(RuntimeError, match="differs"):
        await publisher.publish_async(
            graphs=(changed,),
            contracts=execution.contracts,
            tenant_id=execution.settings.tenant_id,
            workspace_id=None,
        )


def test_repository_publisher_sync_bridge_preserves_exact_graph_identity(sqlite_db) -> None:
    execution = _execution()
    graph = execution.graphs.workflow1

    published = RepositoryTenantGraphPublisher(sqlite_db).publish(
        graphs=(graph,),
        contracts=execution.contracts,
        tenant_id=execution.settings.tenant_id,
        workspace_id=None,
    )

    assert published == (PublishedGraph(graph.graph_id, graph.version),)


class _Process:
    def __init__(self, argv, **kwargs) -> None:
        self.argv = tuple(argv)
        self.kwargs = kwargs
        self.pid = 4321
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def communicate(self, timeout=None):
        return ("service ready\n", "")


def test_local_supervisor_uses_env_not_argv_and_stops_only_owned_process(
    tmp_path: Path,
) -> None:
    processes = []

    def popen(argv, **kwargs):
        process = _Process(argv, **kwargs)
        processes.append(process)
        return process

    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"deployment_ref": "deploy-1", "graph_version_ref": "graph@1"},
            )
        )
    )
    campaign_path = tmp_path / "campaign.json"
    campaign_path.write_text(json.dumps({"campaign_id": "evaluation-studio-v1"}))
    supervisor = LocalDeploymentSupervisor(
        campaign_config=campaign_path,
        evidence_store=EvidenceStore(tmp_path / "evidence"),
        environment={"ZEROTH_SECRET__SIGNING": "value-never-on-argv"},
        client=client,
        popen_factory=popen,
        health_timeout_seconds=1,
        health_poll_seconds=0,
    )

    supervisor.restart(deployment_ref="deploy-1", service_url="http://127.0.0.1:8123")

    assert len(processes) == 1
    assert "value-never-on-argv" not in " ".join(processes[0].argv)
    assert processes[0].kwargs["env"]["ZEROTH_SECRET__SIGNING"] == "value-never-on-argv"
    assert "--host" in processes[0].argv and "127.0.0.1" in processes[0].argv
    assert "--port" in processes[0].argv and "8123" in processes[0].argv
    with pytest.raises(KeyError):
        supervisor.stop("not-owned")
    supervisor.stop("deploy-1")
    assert processes[0].terminated
    assert (tmp_path / "evidence" / "commands" / "0001-service-deploy-1.json").exists()


def test_env_loader_defaults_read_only_and_requires_external_paths_and_exact_ack(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    base = {
        "ZEROTH_EVALUATION_CAMPAIGN_ID": "evaluation-studio-v1",
        "ZEROTH_EVALUATION_ARTIFACT_ROOT": str(external),
        "ZEROTH_EVALUATION_ACTION_SINK_ROOT": str(external / "action-sink"),
    }

    options = load_live_execution_options(base, repository_root=repository)
    assert not options.allow_mutation
    assert not options.allow_provider

    with pytest.raises(ValueError, match="acknowledgement"):
        load_live_execution_options(
            {
                **base,
                "ZEROTH_EVALUATION_ALLOW_PROVIDER": "true",
                "ZEROTH_EVALUATION_PAID_ACK": "wrong",
            },
            repository_root=repository,
        )
    with pytest.raises(ValueError, match="outside"):
        load_live_execution_options(
            {**base, "ZEROTH_EVALUATION_ARTIFACT_ROOT": str(repository / "artifacts")},
            repository_root=repository,
        )


def test_cli_loader_is_inert_by_default_and_provider_gate_requires_exact_ack(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    external = tmp_path / "external"
    default = load_live_execution_cli(
        [
            "--campaign-id",
            "evaluation-studio-v1",
            "--artifact-root",
            str(external),
            "--action-sink-root",
            str(external / "action-sink"),
        ],
        environ={},
        repository_root=repository,
    )
    assert not default.allow_mutation
    assert not default.allow_provider

    with pytest.raises(ValueError, match="acknowledgement"):
        load_live_execution_cli(
            [
                "--campaign-id",
                "evaluation-studio-v1",
                "--artifact-root",
                str(external),
                "--action-sink-root",
                str(external / "action-sink"),
                "--allow-mutation",
                "--allow-provider",
                "--paid-ack",
                "wrong",
            ],
            environ={},
            repository_root=repository,
        )
