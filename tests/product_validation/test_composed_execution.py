"""Provider-independent acceptance for batched subgraph composition."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from zeroth.contracts.governed import RunStatus
from zeroth.contracts.graph import (
    Edge,
    EntrypointNode,
    ExecutionSettings,
    Graph,
    SubgraphNode,
)
from zeroth.contracts.graph.models import ParallelConfig
from zeroth.contracts.graph.serialization import serialize_graph
from zeroth.integrations.execution import ExecutableUnitRunner
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.runtime.orchestration import RuntimeOrchestrator
from zeroth.runtime.parallel.models import BranchContext, GlobalStepTracker
from zeroth.runtime.runs import Run
from zeroth.runtime.subgraphs.executor import SubgraphExecutor
from zeroth.runtime.subgraphs.models import SubgraphNodeData
from zeroth.runtime.subgraphs.resolver import SubgraphResolver
from zeroth.service.deployments.models import Deployment, DeploymentEngineMode

pytestmark = pytest.mark.legacy_engine


class _DeploymentLookup:
    def __init__(self, deployment: Deployment) -> None:
        self.deployment = deployment

    async def get(
        self,
        deployment_ref: str,
        version: int | None = None,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None = None,
    ) -> Deployment | None:
        del workspace_id
        if (
            deployment_ref != self.deployment.deployment_ref
            or tenant_id != self.deployment.tenant_id
            or (version is not None and version != self.deployment.version)
        ):
            return None
        return self.deployment


class _TrackingSubgraphExecutor(SubgraphExecutor):
    """Measure admission at the real subgraph-executor boundary."""

    live: int = 0
    peak: int = 0

    async def execute(
        self,
        orchestrator: RuntimeOrchestrator,
        parent_graph: Graph,
        parent_run: Run,
        node: SubgraphNode,
        node_id: str,
        input_payload: dict[str, Any],
        *,
        branch_context: BranchContext | None = None,
        step_tracker: GlobalStepTracker | None,
    ) -> Run:
        self.live += 1
        self.peak = max(self.peak, self.live)
        try:
            # Hold the admission slot long enough to observe the worker cap;
            # the child itself still executes through the real resolver/run path.
            await asyncio.sleep(0.02)
            return await super().execute(
                orchestrator,
                parent_graph,
                parent_run,
                node,
                node_id,
                input_payload,
                branch_context=branch_context,
                step_tracker=step_tracker,
            )
        finally:
            self.live -= 1


def _child_deployment() -> Deployment:
    child = Graph(
        graph_id="campaign-child",
        name="Campaign deterministic child",
        version=1,
        entry_step="child-entry",
        nodes=[
            EntrypointNode(
                node_id="child-entry",
                graph_version_ref="campaign-child@1",
            )
        ],
        edges=[],
        execution_settings=ExecutionSettings(sequential_join_enabled=False),
    )
    return Deployment(
        deployment_id="campaign-child-deployment-1",
        deployment_ref="campaign-child-deployment",
        version=1,
        graph_id=child.graph_id,
        graph_version=child.version,
        graph_version_ref="campaign-child@1",
        serialized_graph=serialize_graph(child),
        engine_mode=DeploymentEngineMode.LEGACY,
        tenant_id="default",
    )


def _parent_graph() -> Graph:
    source = EntrypointNode(
        node_id="batch-input",
        graph_version_ref="campaign-parent@1",
        parallel_config=ParallelConfig(
            split_path="items",
            merge_strategy="collect",
            fail_mode="fail_fast",
            max_branches=8,
            max_concurrency=4,
            batch_size=8,
        ),
    )
    child = SubgraphNode(
        node_id="investigate-item",
        graph_version_ref="campaign-parent@1",
        subgraph=SubgraphNodeData(
            graph_ref="campaign-child-deployment",
            version=1,
            thread_participation="isolated",
        ),
    )
    return Graph(
        graph_id="campaign-parent",
        name="Campaign deterministic eight-item batch",
        version=1,
        entry_step=source.node_id,
        nodes=[source, child],
        edges=[
            Edge(
                edge_id="batch-to-child",
                source_node_id=source.node_id,
                target_node_id=child.node_id,
            )
        ],
        execution_settings=ExecutionSettings(sequential_join_enabled=False),
    )


async def test_eight_items_run_as_isolated_ordered_children_at_concurrency_four(
    sqlite_db,
) -> None:
    """One parent creates eight real child runs and preserves input ordering."""
    repository = RunRepository.for_default_compatibility(sqlite_db)
    resolver = SubgraphResolver(_DeploymentLookup(_child_deployment()))
    subgraphs = _TrackingSubgraphExecutor(resolver=resolver)
    orchestrator = RuntimeOrchestrator(
        run_repository=repository,
        agent_runners={},
        executable_unit_runner=ExecutableUnitRunner(),
        subgraph_executor=subgraphs,
    )
    items = [{"index": index, "question": f"case-{index}"} for index in range(8)]

    parent = await orchestrator.run_graph(_parent_graph(), {"items": items})

    assert parent.status is RunStatus.COMPLETED
    assert subgraphs.peak == 4
    assert parent.final_output == {"items": items}

    children = await repository.list_runs("campaign-child-deployment", limit=20)
    assert len(children) == 8
    assert {child.parent_run_id for child in children} == {parent.run_id}
    assert len({child.thread_id for child in children}) == 8
    assert all(child.status is RunStatus.COMPLETED for child in children)
    assert all(child.metadata["subgraph_depth"] == 1 for child in children)
    assert all(child.metadata["total_cost_usd"] == 0.0 for child in children)
    assert all(child.metadata["total_estimated_cost_usd"] == 0.0 for child in children)
    assert all(
        child.execution_history[0].node_id.startswith("branch:")
        and ":subgraph:campaign-child-deployment:1:child-entry"
        in child.execution_history[0].node_id
        for child in children
    )
