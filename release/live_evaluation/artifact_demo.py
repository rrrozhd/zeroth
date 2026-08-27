"""Tenant-scoped workflow fixture that emits real artifacts through the runtime."""

from __future__ import annotations

from zeroth.contracts.graph import (
    ExecutableUnitNode,
    ExecutableUnitNodeData,
    ExecutionSettings,
    Graph,
)

from .action_runner import (
    EVALUATION_ARTIFACT_MANIFEST_REF,
    EvaluationArtifactOutput,
    EvaluationArtifactPayload,
)
from .campaign_execution import ContractSpec
from .campaign_runtime import RepositoryTenantGraphPublisher

ARTIFACT_DEMO_GRAPH_ID = "evaluation-studio-v1-artifact-output"
ARTIFACT_DEMO_DEPLOYMENT_REF = "demo-artifact-output-v1"
ARTIFACT_INPUT_CONTRACT = "evaluation-studio-v1.artifact-output.input@1"
ARTIFACT_OUTPUT_CONTRACT = "evaluation-studio-v1.artifact-output.output@1"


def build_artifact_demo_graph(*, tenant_id: str, workspace_id: str | None = None) -> Graph:
    """Build the single-node workflow used to prove store/UI lifecycle behavior."""
    return Graph(
        graph_id=ARTIFACT_DEMO_GRAPH_ID,
        version=2,
        name="Artifact output showcase",
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        entry_step="emit-artifact",
        nodes=[
            ExecutableUnitNode(
                node_id="emit-artifact",
                graph_version_ref=f"{ARTIFACT_DEMO_GRAPH_ID}@2",
                input_contract_ref=ARTIFACT_INPUT_CONTRACT,
                output_contract_ref=ARTIFACT_OUTPUT_CONTRACT,
                execution_config={
                    "instrumentation": {
                        "campaign_id": "{{campaign_id}}",
                        "run_id": "{{run_id}}",
                        "operation_id": "{{operation_id}}",
                    }
                },
                executable_unit=ExecutableUnitNodeData(
                    manifest_ref=EVALUATION_ARTIFACT_MANIFEST_REF,
                    execution_mode="native",
                    timeout_seconds=10,
                    output_extraction_strategy="json_stdout",
                ),
            )
        ],
        edges=[],
        execution_settings=ExecutionSettings(
            max_total_steps=2,
            max_total_runtime_seconds=20,
            max_visits_per_node=1,
            audit_enabled=True,
        ),
        metadata={"evaluation_workflow": "artifact-output", "external_calls": False},
    )


async def seed_artifact_demo(
    database,
    *,
    tenant_id: str,
    workspace_id: str | None = None,
):
    """Publish and register the artifact workflow without duplicating a deployment."""
    publisher = RepositoryTenantGraphPublisher(database)
    published = await publisher.publish_async(
        graphs=(build_artifact_demo_graph(tenant_id=tenant_id, workspace_id=workspace_id),),
        contracts=(
            ContractSpec(ARTIFACT_INPUT_CONTRACT, EvaluationArtifactPayload),
            ContractSpec(ARTIFACT_OUTPUT_CONTRACT, EvaluationArtifactOutput),
        ),
        tenant_id=tenant_id,
        workspace_id=workspace_id,
    )
    graph = published[0]
    existing = await publisher.deployment_repository.get(
        ARTIFACT_DEMO_DEPLOYMENT_REF,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
    )
    if existing is not None and (existing.graph_id, existing.graph_version) == (
        graph.graph_id,
        graph.version,
    ):
        return existing
    return await publisher.deployment_service(tenant_id, workspace_id).deploy(
        ARTIFACT_DEMO_DEPLOYMENT_REF,
        graph.graph_id,
        graph.version,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
    )


__all__ = [
    "ARTIFACT_DEMO_DEPLOYMENT_REF",
    "ARTIFACT_DEMO_GRAPH_ID",
    "build_artifact_demo_graph",
    "seed_artifact_demo",
]
