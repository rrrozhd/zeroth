"""Real published subgraphs across a structured-token approval checkpoint."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from zeroth.contracts.graph import (
    Edge,
    EntrypointNode,
    Graph,
    GraphRepository,
    HumanApprovalNode,
    HumanApprovalNodeData,
    SubgraphNode,
    SubgraphNodeData,
)
from zeroth.contracts.mappings import ConstantMappingOperation, EdgeMapping
from zeroth.contracts.registry import ContractRegistry, contract_scope_context
from zeroth.governance.approvals import (
    ApprovalDecision,
    ApprovalRepository,
    ApprovalService,
)
from zeroth.governance.audit import AuditRepository
from zeroth.governance.identity import ActorIdentity, AuthMethod
from zeroth.integrations.execution import ExecutableUnitRegistry, ExecutableUnitRunner
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.runtime.graph_validation import GraphValidator
from zeroth.runtime.orchestration import RuntimeOrchestrator
from zeroth.runtime.parallel.models import JoinConfig
from zeroth.runtime.runs import RunStatus
from zeroth.runtime.subgraphs import SubgraphExecutor
from zeroth.runtime.subgraphs.resolver import (
    SubgraphResolver,
    merge_governance,
    namespace_subgraph,
)
from zeroth.service.deployments import DeploymentService, SQLiteDeploymentRepository
from zeroth.platform.signing import EnvHmacSigner


class CheckpointPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    request: str | None = None
    branch: str | None = None
    branches: list[dict[str, str | None]] | None = None


def _contract_node(node):
    node.input_contract_ref = "contract://checkpoint"
    node.output_contract_ref = "contract://checkpoint"
    return node


def _entry_graph(graph_id: str) -> Graph:
    return Graph(
        graph_id=graph_id,
        name=graph_id,
        entry_step="entry",
        nodes=[_contract_node(EntrypointNode(node_id="entry", graph_version_ref=f"{graph_id}@1"))],
        edges=[],
    )


def _approval_graph(graph_id: str) -> Graph:
    return Graph(
        graph_id=graph_id,
        name=graph_id,
        entry_step="approve",
        nodes=[
            _contract_node(
                HumanApprovalNode(
                    node_id="approve",
                    graph_version_ref=f"{graph_id}@1",
                    human_approval=HumanApprovalNodeData(
                        approval_payload_schema_ref="contract://checkpoint",
                        resolution_schema_ref="contract://checkpoint",
                        approval_policy_config={"allow_edits": True},
                    ),
                ),
            )
        ],
        edges=[],
    )


def _parent_graph() -> Graph:
    graph_id = "checkpoint-parent"
    entry = _contract_node(EntrypointNode(node_id="entry", graph_version_ref=f"{graph_id}@1"))
    durable = _contract_node(
        SubgraphNode(
            node_id="durable-child",
            graph_version_ref=f"{graph_id}@1",
            subgraph=SubgraphNodeData(graph_ref="checkpoint-child-durable"),
        )
    )
    gated = _contract_node(
        SubgraphNode(
            node_id="approval-child",
            graph_version_ref=f"{graph_id}@1",
            subgraph=SubgraphNodeData(graph_ref="checkpoint-child-approval"),
        )
    )
    collector = _contract_node(
        SubgraphNode(
            node_id="collector",
            graph_version_ref=f"{graph_id}@1",
            join_config=JoinConfig(merge_path="branches"),
            subgraph=SubgraphNodeData(graph_ref="checkpoint-child-collector"),
        )
    )
    return Graph(
        graph_id=graph_id,
        name="Provider-free child pause checkpoint",
        entry_step="entry",
        nodes=[entry, durable, gated, collector],
        edges=[
            Edge(
                edge_id="entry-durable",
                source_node_id="entry",
                target_node_id="durable-child",
                mapping=EdgeMapping(
                    operations=[ConstantMappingOperation(target_path="branch", value="durable")]
                ),
            ),
            Edge(
                edge_id="entry-approval",
                source_node_id="entry",
                target_node_id="approval-child",
                mapping=EdgeMapping(
                    operations=[ConstantMappingOperation(target_path="branch", value="approval")]
                ),
            ),
            Edge(
                edge_id="durable-collector",
                source_node_id="durable-child",
                target_node_id="collector",
            ),
            Edge(
                edge_id="approval-collector",
                source_node_id="approval-child",
                target_node_id="collector",
            ),
        ],
    )


async def _publish_and_deploy(
    graph_repository: GraphRepository,
    deployment_service: DeploymentService,
    graph: Graph,
    deployment_ref: str,
) -> Graph:
    saved = await graph_repository.create(graph)
    published = await graph_repository.publish(saved.graph_id, saved.version)
    await deployment_service.deploy(deployment_ref, published.graph_id, published.version)
    return published


async def test_published_subgraph_pause_restores_partial_collect_and_resumes(
    sqlite_db,
) -> None:
    """One real child pauses while the sibling's join delivery stays durable."""
    contract_registry = ContractRegistry.scoped(sqlite_db, contract_scope_context("default", None))
    await contract_registry.register(CheckpointPayload, name="contract://checkpoint")
    graph_repository = GraphRepository(
        sqlite_db, validator=GraphValidator(contract_registry=contract_registry)
    )
    deployment_service = DeploymentService(
        graph_repository=graph_repository,
        deployment_repository=SQLiteDeploymentRepository(sqlite_db),
        contract_registry=contract_registry,
    )
    await _publish_and_deploy(
        graph_repository,
        deployment_service,
        _entry_graph("checkpoint-durable-graph"),
        "checkpoint-child-durable",
    )
    await _publish_and_deploy(
        graph_repository,
        deployment_service,
        _approval_graph("checkpoint-approval-graph"),
        "checkpoint-child-approval",
    )
    await _publish_and_deploy(
        graph_repository,
        deployment_service,
        _entry_graph("checkpoint-collector-graph"),
        "checkpoint-child-collector",
    )
    parent_graph = await _publish_and_deploy(
        graph_repository,
        deployment_service,
        _parent_graph(),
        "checkpoint-parent",
    )

    run_repository = RunRepository.for_default_compatibility(sqlite_db)
    signer = EnvHmacSigner(
        key_id="checkpoint-child-continuation",
        keys={"checkpoint-child-continuation": b"test-key"},
    )
    audit_repository = AuditRepository.for_default_compatibility(sqlite_db, signer=signer)
    approval_service = ApprovalService(
        repository=ApprovalRepository(sqlite_db),
        run_repository=run_repository,
        audit_repository=audit_repository,
    )
    resolver = SubgraphResolver(deployment_service=deployment_service)
    orchestrator = RuntimeOrchestrator(
        run_repository=run_repository,
        agent_runners={},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
        approval_service=approval_service,
        subgraph_executor=SubgraphExecutor(resolver=resolver),
    )

    paused = await orchestrator.run_graph(parent_graph, {"request": "checkpoint"})

    assert paused.status is RunStatus.WAITING_APPROVAL
    assert paused.failure_state is None
    assert paused.metadata["pending_subgraph"]["node_id"] == "approval-child"
    pending = await approval_service.list_pending(tenant_id="default", workspace_id=None)
    assert len(pending) == 1
    approval = pending[0]
    assert approval.node_id.endswith("subgraph:checkpoint-child-approval:1:approve")
    assert approval.proposed_payload == {"branch": "approval"}

    # Re-open both durable documents through a fresh repository object. The
    # completed sibling is a delivered join obligation; the gated sibling is
    # still the sole in-flight token claim and must not be reported as failed.
    reloaded_repository = RunRepository.for_default_compatibility(sqlite_db)
    reloaded_parent = await reloaded_repository.get(paused.run_id)
    snapshot = await reloaded_repository.get_token_snapshot(paused.run_id)
    assert reloaded_parent is not None
    assert reloaded_parent.status is RunStatus.WAITING_APPROVAL
    assert reloaded_parent.failure_state is None
    assert snapshot is not None
    assert len(snapshot.in_flight_dispatches) == 1
    partial_join = next(join for join in snapshot.joins if join.target_node_id == "collector")
    assert partial_join.delivered_obligation_count == 1
    delivered = next(item for item in partial_join.obligations if item.delivery is not None)
    assert delivered.delivery.payload == {"branch": "durable"}
    assert sum(item.outcome is None for item in partial_join.obligations) == 1

    await approval_service.resolve(
        approval.approval_id,
        decision=ApprovalDecision.EDIT_AND_APPROVE,
        actor=ActorIdentity(subject="checkpoint-reviewer", auth_method=AuthMethod.API_KEY),
        edited_payload={"branch": "approval"},
    )
    scheduled_parent = await approval_service.schedule_ancestor_continuation(
        approval.approval_id,
        deployment_ref="checkpoint-parent",
        graph_version_ref=f"{parent_graph.graph_id}:v{parent_graph.version}",
    )
    assert scheduled_parent.run_id == paused.run_id
    assert scheduled_parent.status is RunStatus.PENDING

    resumed_parent = await orchestrator.resume_graph(parent_graph, paused.run_id)

    assert resumed_parent.status is RunStatus.COMPLETED
    assert resumed_parent.failure_state is None
    assert resumed_parent.final_output == {
        "branches": [{"branch": "durable"}, {"branch": "approval"}]
    }
    assert "pending_subgraph" not in resumed_parent.metadata
    completed_snapshot = await run_repository.get_token_snapshot(paused.run_id)
    assert completed_snapshot is not None
    assert completed_snapshot.state.value == "completed"
    child_run = await run_repository.get(approval.run_id)
    assert child_run is not None
    assert child_run.status is RunStatus.COMPLETED
    notifications = [
        record
        for record in await audit_repository.list_by_run(paused.run_id)
        if record.status == "child_approval_continuation_scheduled"
    ]
    assert len(notifications) == 1
    assert notifications[0].record_signature is not None


async def test_unresolvable_published_child_fails_closed_without_approval(
    sqlite_db,
) -> None:
    """Resolution failure is terminal evidence, never an approval-shaped pause."""
    contract_registry = ContractRegistry.scoped(sqlite_db, contract_scope_context("default", None))
    await contract_registry.register(CheckpointPayload, name="contract://checkpoint")
    graph_repository = GraphRepository(
        sqlite_db, validator=GraphValidator(contract_registry=contract_registry)
    )
    deployment_service = DeploymentService(
        graph_repository=graph_repository,
        deployment_repository=SQLiteDeploymentRepository(sqlite_db),
        contract_registry=contract_registry,
    )
    graph_id = "checkpoint-missing-child-parent"
    parent_graph = await _publish_and_deploy(
        graph_repository,
        deployment_service,
        Graph(
            graph_id=graph_id,
            name="Missing child fails closed",
            entry_step="entry",
            nodes=[
                _contract_node(EntrypointNode(node_id="entry", graph_version_ref=f"{graph_id}@1")),
                _contract_node(
                    SubgraphNode(
                        node_id="missing-child",
                        graph_version_ref=f"{graph_id}@1",
                        subgraph=SubgraphNodeData(graph_ref="does-not-exist"),
                    )
                ),
            ],
            edges=[
                Edge(
                    edge_id="entry-missing",
                    source_node_id="entry",
                    target_node_id="missing-child",
                )
            ],
        ),
        "checkpoint-missing-child-parent",
    )
    run_repository = RunRepository.for_default_compatibility(sqlite_db)
    approval_service = ApprovalService(
        repository=ApprovalRepository(sqlite_db),
        run_repository=run_repository,
        audit_repository=AuditRepository.for_default_compatibility(sqlite_db),
    )
    orchestrator = RuntimeOrchestrator(
        run_repository=run_repository,
        agent_runners={},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
        approval_service=approval_service,
        subgraph_executor=SubgraphExecutor(
            resolver=SubgraphResolver(deployment_service=deployment_service)
        ),
    )

    failed = await orchestrator.run_graph(parent_graph, {"request": "checkpoint"})

    assert failed.status is RunStatus.FAILED
    assert failed.failure_state is not None
    assert failed.failure_state.reason == "node_execution_failed"
    assert "not found" in failed.failure_state.message
    assert "pending_subgraph" not in failed.metadata
    assert await approval_service.list_pending(tenant_id="default", workspace_id=None) == []
    snapshot = await run_repository.get_token_snapshot(failed.run_id)
    assert snapshot is not None
    # Failed-run replay deliberately retains the exact claim. It is not a
    # scheduler PAUSE: the public run verdict is FAILED, there is no approval,
    # and an operator must explicitly transition the run before this claim can
    # be recovered.
    assert snapshot.state.value == "running"
    assert len(snapshot.in_flight_dispatches) == 1
    assert snapshot.cancellation_fence is None
