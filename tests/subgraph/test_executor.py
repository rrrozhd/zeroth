"""Tests for SubgraphExecutor -- child Run creation and recursive _drive()."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from zeroth.contracts.graph.models import (
    AgentNode,
    AgentNodeData,
    ExecutionSettings,
    Graph,
    SubgraphNode,
)
from zeroth.contracts.graph.serialization import serialize_graph
from zeroth.runtime.runs import Run, RunStatus
from zeroth.runtime.subgraphs.errors import (
    SubgraphCycleError,
    SubgraphDepthLimitError,
    SubgraphExecutionError,
)
from zeroth.runtime.subgraphs.executor import SubgraphExecutor
from zeroth.runtime.subgraphs.models import SubgraphNodeData
from zeroth.runtime.subgraphs.resolver import SubgraphResolver
from zeroth.service.deployments.models import Deployment

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_child_graph(
    graph_id: str = "child-g",
    entry_step: str = "c1",
) -> Graph:
    """Create a simple child graph for testing."""
    node = AgentNode(
        node_id="c1",
        graph_version_ref=f"{graph_id}@1",
        agent=AgentNodeData(
            instruction="child task",
            model_provider="openai/gpt-4",
        ),
    )
    return Graph(
        graph_id=graph_id,
        name="child-workflow",
        version=1,
        nodes=[node],
        edges=[],
        entry_step=entry_step,
    )


def _make_parent_graph(
    graph_id: str = "parent-g",
    entry_step: str = "s1",
) -> Graph:
    """Create a parent graph with a SubgraphNode."""
    subgraph_node = SubgraphNode(
        node_id="s1",
        graph_version_ref=f"{graph_id}@1",
        subgraph=SubgraphNodeData(graph_ref="child-g"),
    )
    return Graph(
        graph_id=graph_id,
        name="parent-workflow",
        version=1,
        nodes=[subgraph_node],
        edges=[],
        entry_step=entry_step,
    )


def _make_parent_run(
    thread_id: str = "thread-1",
    metadata: dict | None = None,
) -> Run:
    """Create a parent Run for testing."""
    return Run(
        run_id="parent-run-1",
        graph_version_ref="parent-g:v1",
        deployment_ref="parent-g",
        thread_id=thread_id,
        tenant_id="tenant-1",
        workspace_id="ws-1",
        status=RunStatus.RUNNING,
        metadata=metadata or {},
    )


def _make_child_deployment(graph: Graph) -> Deployment:
    """Create a Deployment wrapping the child graph."""
    return Deployment(
        deployment_id="dep-child-1",
        deployment_ref="child-g",
        graph_id=graph.graph_id,
        graph_version=graph.version,
        graph_version_ref=f"{graph.graph_id}:v{graph.version}",
        serialized_graph=serialize_graph(graph),
    )


def _make_executor(resolver: SubgraphResolver | None = None) -> SubgraphExecutor:
    """Create a SubgraphExecutor with a mock resolver."""
    return SubgraphExecutor(resolver=resolver or MagicMock(spec=SubgraphResolver))


def _make_orchestrator(
    child_run_result: Run | None = None,
    entry_step: str = "subgraph:child-g:1:c1",
) -> MagicMock:
    """Create a mock orchestrator with _drive(), _entry_step(), run_repository."""
    orch = MagicMock()
    orch.run_repository = AsyncMock()
    orch.run_repository.create = AsyncMock(side_effect=lambda r: r)
    orch.run_repository.put = AsyncMock(side_effect=lambda r: r)
    orch.run_repository.write_checkpoint = AsyncMock()
    orch._entry_step = MagicMock(return_value=entry_step)
    orch._initial_metadata = MagicMock(
        side_effect=lambda graph, payload: {
            "graph_id": graph.graph_id,
            "graph_name": graph.name,
            "edge_visit_counts": {},
            "path": [],
            "audits": {},
            "initial_input": dict(payload),
            "node_payloads": {},
            "node_tags": {},
        }
    )

    if child_run_result is not None:
        orch._drive = AsyncMock(return_value=child_run_result)
    else:
        # Default: return a completed child run
        async def _drive_side_effect(graph, run, *, step_tracker=None):
            run.status = RunStatus.COMPLETED
            run.final_output = {"result": "child-done"}
            return run

        orch._drive = AsyncMock(side_effect=_drive_side_effect)

    return orch


# ---------------------------------------------------------------------------
# Tests: Happy path
# ---------------------------------------------------------------------------


class TestSubgraphExecutorHappyPath:
    """execute() resolves, namespaces, merges, creates the child Run, and calls _drive()."""

    @pytest.mark.asyncio
    async def test_execute_creates_child_run_with_parent_run_id(self) -> None:
        child_graph = _make_child_graph()
        deployment = _make_child_deployment(child_graph)
        resolver = MagicMock(spec=SubgraphResolver)
        resolver.resolve = AsyncMock(return_value=(child_graph, deployment))

        executor = SubgraphExecutor(resolver=resolver)
        orch = _make_orchestrator()
        parent_graph = _make_parent_graph()
        parent_run = _make_parent_run()
        node = parent_graph.nodes[0]

        await executor.execute(
            orchestrator=orch,
            parent_graph=parent_graph,
            parent_run=parent_run,
            node=node,
            node_id="s1",
            input_payload={"query": "hello"},
            step_tracker=None,
        )

        # Child run should have parent_run_id set
        create_call_args = orch.run_repository.create.call_args
        child_run_arg = create_call_args[0][0]
        assert child_run_arg.parent_run_id == "parent-run-1"

    @pytest.mark.asyncio
    async def test_execute_inherit_thread_shares_parent_thread_id(self) -> None:
        child_graph = _make_child_graph()
        deployment = _make_child_deployment(child_graph)
        resolver = MagicMock(spec=SubgraphResolver)
        resolver.resolve = AsyncMock(return_value=(child_graph, deployment))

        executor = SubgraphExecutor(resolver=resolver)
        orch = _make_orchestrator()
        parent_graph = _make_parent_graph()
        parent_run = _make_parent_run(thread_id="parent-thread-42")
        node = parent_graph.nodes[0]

        await executor.execute(
            orchestrator=orch,
            parent_graph=parent_graph,
            parent_run=parent_run,
            node=node,
            node_id="s1",
            input_payload={},
            step_tracker=None,
        )

        child_run_arg = orch.run_repository.create.call_args[0][0]
        assert child_run_arg.thread_id == "parent-thread-42"

    @pytest.mark.asyncio
    async def test_execute_isolated_thread_gets_empty_thread_id(self) -> None:
        child_graph = _make_child_graph()
        deployment = _make_child_deployment(child_graph)
        resolver = MagicMock(spec=SubgraphResolver)
        resolver.resolve = AsyncMock(return_value=(child_graph, deployment))

        executor = SubgraphExecutor(resolver=resolver)
        orch = _make_orchestrator()
        parent_graph = Graph(
            graph_id="parent-g",
            name="parent-workflow",
            version=1,
            nodes=[
                SubgraphNode(
                    node_id="s1",
                    graph_version_ref="parent-g@1",
                    subgraph=SubgraphNodeData(
                        graph_ref="child-g",
                        thread_participation="isolated",
                    ),
                )
            ],
            edges=[],
            entry_step="s1",
        )
        parent_run = _make_parent_run(thread_id="parent-thread-42")
        node = parent_graph.nodes[0]

        await executor.execute(
            orchestrator=orch,
            parent_graph=parent_graph,
            parent_run=parent_run,
            node=node,
            node_id="s1",
            input_payload={},
            step_tracker=None,
        )

        child_run_arg = orch.run_repository.create.call_args[0][0]
        # Empty string triggers auto-generation in Run model validator
        assert child_run_arg.thread_id != "parent-thread-42"

    @pytest.mark.asyncio
    async def test_execute_child_run_metadata_contains_depth_and_lineage(self) -> None:
        child_graph = _make_child_graph()
        deployment = _make_child_deployment(child_graph)
        resolver = MagicMock(spec=SubgraphResolver)
        resolver.resolve = AsyncMock(return_value=(child_graph, deployment))

        executor = SubgraphExecutor(resolver=resolver)
        orch = _make_orchestrator()
        parent_graph = _make_parent_graph()
        parent_run = _make_parent_run()
        node = parent_graph.nodes[0]

        await executor.execute(
            orchestrator=orch,
            parent_graph=parent_graph,
            parent_run=parent_run,
            node=node,
            node_id="s1",
            input_payload={"x": 1},
            step_tracker=None,
        )

        child_run_arg = orch.run_repository.create.call_args[0][0]
        assert child_run_arg.metadata["subgraph_depth"] == 1
        assert child_run_arg.metadata["parent_run_id"] == "parent-run-1"
        assert child_run_arg.metadata["parent_node_id"] == "s1"

    @pytest.mark.asyncio
    async def test_execute_child_run_inherits_strict_campaign_identity(self) -> None:
        child_graph = _make_child_graph()
        deployment = _make_child_deployment(child_graph)
        resolver = MagicMock(spec=SubgraphResolver)
        resolver.resolve = AsyncMock(return_value=(child_graph, deployment))
        executor = SubgraphExecutor(resolver=resolver)
        orch = _make_orchestrator()
        parent_graph = _make_parent_graph()
        parent_run = _make_parent_run(
            metadata={"campaign_id": "campaign-live-1", "campaign_strict": True}
        )

        await executor.execute(
            orchestrator=orch,
            parent_graph=parent_graph,
            parent_run=parent_run,
            node=parent_graph.nodes[0],
            node_id="s1",
            input_payload={"x": 1},
            step_tracker=None,
        )

        child_run = orch.run_repository.create.call_args[0][0]
        assert child_run.metadata["campaign_id"] == "campaign-live-1"
        assert child_run.metadata["campaign_strict"] is True

    @pytest.mark.asyncio
    async def test_execute_token_child_seeds_structured_initial_input_without_legacy_queue(
        self,
    ) -> None:
        child_graph = _make_child_graph()
        deployment = _make_child_deployment(child_graph)
        resolver = MagicMock(spec=SubgraphResolver)
        resolver.resolve = AsyncMock(return_value=(child_graph, deployment))

        executor = SubgraphExecutor(resolver=resolver)
        orch = _make_orchestrator()
        parent_graph = _make_parent_graph()
        parent_run = _make_parent_run()

        await executor.execute(
            orchestrator=orch,
            parent_graph=parent_graph,
            parent_run=parent_run,
            node=parent_graph.nodes[0],
            node_id="s1",
            input_payload={"index": 3, "value": "deterministic-item-3"},
            step_tracker=None,
        )

        child_run = orch.run_repository.create.call_args.args[0]
        assert child_run.metadata["initial_input"] == {
            "index": 3,
            "value": "deterministic-item-3",
        }
        assert child_run.metadata["node_payloads"] == {}
        assert child_run.pending_node_ids == []

    @pytest.mark.asyncio
    async def test_execute_legacy_child_preserves_entry_queue_and_payload(self) -> None:
        child_graph = _make_child_graph().model_copy(
            update={"execution_settings": ExecutionSettings(sequential_join_enabled=False)}
        )
        deployment = _make_child_deployment(child_graph)
        resolver = MagicMock(spec=SubgraphResolver)
        resolver.resolve = AsyncMock(return_value=(child_graph, deployment))

        executor = SubgraphExecutor(resolver=resolver)
        orch = _make_orchestrator()
        orch._initial_metadata = MagicMock(
            return_value={
                "graph_id": child_graph.graph_id,
                "graph_name": child_graph.name,
                "edge_visit_counts": {},
                "path": [],
                "audits": {},
                "node_payloads": {
                    "subgraph:child-g:1:c1": {
                        "index": 4,
                        "value": "deterministic-item-4",
                    }
                },
            }
        )
        parent_graph = _make_parent_graph()
        parent_run = _make_parent_run()

        await executor.execute(
            orchestrator=orch,
            parent_graph=parent_graph,
            parent_run=parent_run,
            node=parent_graph.nodes[0],
            node_id="s1",
            input_payload={"index": 4, "value": "deterministic-item-4"},
            step_tracker=None,
        )

        child_run = orch.run_repository.create.call_args.args[0]
        assert child_run.pending_node_ids == ["subgraph:child-g:1:c1"]
        assert child_run.metadata["node_payloads"] == {
            "subgraph:child-g:1:c1": {
                "index": 4,
                "value": "deterministic-item-4",
            }
        }
        assert "initial_input" not in child_run.metadata

    @pytest.mark.asyncio
    async def test_execute_returns_child_run(self) -> None:
        child_graph = _make_child_graph()
        deployment = _make_child_deployment(child_graph)
        resolver = MagicMock(spec=SubgraphResolver)
        resolver.resolve = AsyncMock(return_value=(child_graph, deployment))

        executor = SubgraphExecutor(resolver=resolver)
        orch = _make_orchestrator()
        parent_graph = _make_parent_graph()
        parent_run = _make_parent_run()
        node = parent_graph.nodes[0]

        result = await executor.execute(
            orchestrator=orch,
            parent_graph=parent_graph,
            parent_run=parent_run,
            node=node,
            node_id="s1",
            input_payload={},
            step_tracker=None,
        )

        assert result.status == RunStatus.COMPLETED
        assert result.final_output == {"result": "child-done"}

    @pytest.mark.asyncio
    async def test_execute_calls_drive_with_merged_subgraph(self) -> None:
        child_graph = _make_child_graph()
        deployment = _make_child_deployment(child_graph)
        resolver = MagicMock(spec=SubgraphResolver)
        resolver.resolve = AsyncMock(return_value=(child_graph, deployment))

        executor = SubgraphExecutor(resolver=resolver)
        orch = _make_orchestrator()
        parent_graph = _make_parent_graph()
        parent_run = _make_parent_run()
        node = parent_graph.nodes[0]

        await executor.execute(
            orchestrator=orch,
            parent_graph=parent_graph,
            parent_run=parent_run,
            node=node,
            node_id="s1",
            input_payload={},
            step_tracker=None,
        )

        # _drive() should have been called
        orch._drive.assert_called_once()
        drive_graph_arg = orch._drive.call_args[0][0]
        # The graph should have namespaced node IDs
        assert any("subgraph:child-g:" in n.node_id for n in drive_graph_arg.nodes)


# ---------------------------------------------------------------------------
# Tests: Depth tracking
# ---------------------------------------------------------------------------


class TestSubgraphExecutorDepthTracking:
    """Depth tracking increments per nesting level and raises SubgraphDepthLimitError."""

    @pytest.mark.asyncio
    async def test_depth_increments_from_parent_metadata(self) -> None:
        child_graph = _make_child_graph()
        deployment = _make_child_deployment(child_graph)
        resolver = MagicMock(spec=SubgraphResolver)
        resolver.resolve = AsyncMock(return_value=(child_graph, deployment))

        executor = SubgraphExecutor(resolver=resolver)
        orch = _make_orchestrator()
        parent_graph = _make_parent_graph()
        parent_run = _make_parent_run(metadata={"subgraph_depth": 2})
        node = parent_graph.nodes[0]

        await executor.execute(
            orchestrator=orch,
            parent_graph=parent_graph,
            parent_run=parent_run,
            node=node,
            node_id="s1",
            input_payload={},
            step_tracker=None,
        )

        child_run_arg = orch.run_repository.create.call_args[0][0]
        assert child_run_arg.metadata["subgraph_depth"] == 3

    @pytest.mark.asyncio
    async def test_depth_limit_exceeded_raises_error(self) -> None:
        resolver = MagicMock(spec=SubgraphResolver)
        executor = SubgraphExecutor(resolver=resolver)
        orch = _make_orchestrator()

        parent_graph = Graph(
            graph_id="parent-g",
            name="parent-workflow",
            version=1,
            nodes=[
                SubgraphNode(
                    node_id="s1",
                    graph_version_ref="parent-g@1",
                    subgraph=SubgraphNodeData(graph_ref="child-g", max_depth=2),
                )
            ],
            edges=[],
            entry_step="s1",
        )
        # Parent already at depth 2, and max_depth is 2
        parent_run = _make_parent_run(metadata={"subgraph_depth": 2})
        node = parent_graph.nodes[0]

        with pytest.raises(SubgraphDepthLimitError, match="depth 3 exceeds max_depth 2"):
            await executor.execute(
                orchestrator=orch,
                parent_graph=parent_graph,
                parent_run=parent_run,
                node=node,
                node_id="s1",
                input_payload={},
                step_tracker=None,
            )

        # resolver.resolve should NOT have been called
        resolver.resolve.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: Cycle detection
# ---------------------------------------------------------------------------


class TestSubgraphExecutorCycleDetection:
    """Cycle detection raises SubgraphCycleError when a graph_ref appears in the recursion chain."""

    @pytest.mark.asyncio
    async def test_cycle_detected_raises_error(self) -> None:
        resolver = MagicMock(spec=SubgraphResolver)
        executor = SubgraphExecutor(resolver=resolver)
        orch = _make_orchestrator()
        parent_graph = _make_parent_graph()
        parent_run = _make_parent_run(metadata={"visited_subgraph_refs": ["child-g"]})
        node = parent_graph.nodes[0]

        with pytest.raises(SubgraphCycleError, match="circular subgraph reference"):
            await executor.execute(
                orchestrator=orch,
                parent_graph=parent_graph,
                parent_run=parent_run,
                node=node,
                node_id="s1",
                input_payload={},
                step_tracker=None,
            )

    @pytest.mark.asyncio
    async def test_no_cycle_when_ref_not_visited(self) -> None:
        child_graph = _make_child_graph()
        deployment = _make_child_deployment(child_graph)
        resolver = MagicMock(spec=SubgraphResolver)
        resolver.resolve = AsyncMock(return_value=(child_graph, deployment))

        executor = SubgraphExecutor(resolver=resolver)
        orch = _make_orchestrator()
        parent_graph = _make_parent_graph()
        parent_run = _make_parent_run(metadata={"visited_subgraph_refs": ["other-graph"]})
        node = parent_graph.nodes[0]

        # Should not raise
        result = await executor.execute(
            orchestrator=orch,
            parent_graph=parent_graph,
            parent_run=parent_run,
            node=node,
            node_id="s1",
            input_payload={},
            step_tracker=None,
        )
        assert result is not None

    @pytest.mark.asyncio
    async def test_visited_refs_passed_to_child(self) -> None:
        child_graph = _make_child_graph()
        deployment = _make_child_deployment(child_graph)
        resolver = MagicMock(spec=SubgraphResolver)
        resolver.resolve = AsyncMock(return_value=(child_graph, deployment))

        executor = SubgraphExecutor(resolver=resolver)
        orch = _make_orchestrator()
        parent_graph = _make_parent_graph()
        parent_run = _make_parent_run(metadata={"visited_subgraph_refs": ["other-graph"]})
        node = parent_graph.nodes[0]

        await executor.execute(
            orchestrator=orch,
            parent_graph=parent_graph,
            parent_run=parent_run,
            node=node,
            node_id="s1",
            input_payload={},
            step_tracker=None,
        )

        child_run_arg = orch.run_repository.create.call_args[0][0]
        visited = child_run_arg.metadata["visited_subgraph_refs"]
        assert "other-graph" in visited
        assert "child-g" in visited


# ---------------------------------------------------------------------------
# Tests: Error handling
# ---------------------------------------------------------------------------


class TestSubgraphExecutorErrors:
    """SubgraphExecutionError wraps unexpected exceptions from _drive()."""

    @pytest.mark.asyncio
    async def test_drive_exception_wrapped_in_subgraph_execution_error(self) -> None:
        child_graph = _make_child_graph()
        deployment = _make_child_deployment(child_graph)
        resolver = MagicMock(spec=SubgraphResolver)
        resolver.resolve = AsyncMock(return_value=(child_graph, deployment))

        executor = SubgraphExecutor(resolver=resolver)
        orch = _make_orchestrator()
        orch._drive = AsyncMock(side_effect=RuntimeError("unexpected boom"))
        parent_graph = _make_parent_graph()
        parent_run = _make_parent_run()
        node = parent_graph.nodes[0]

        with pytest.raises(SubgraphExecutionError, match="unexpected boom"):
            await executor.execute(
                orchestrator=orch,
                parent_graph=parent_graph,
                parent_run=parent_run,
                node=node,
                node_id="s1",
                input_payload={},
                step_tracker=None,
            )

    @pytest.mark.asyncio
    async def test_orchestrator_none_raises_clear_error(self) -> None:
        executor = _make_executor()
        parent_graph = _make_parent_graph()
        parent_run = _make_parent_run()
        node = parent_graph.nodes[0]

        with pytest.raises(SubgraphExecutionError, match="orchestrator"):
            await executor.execute(
                orchestrator=None,  # type: ignore[arg-type]
                parent_graph=parent_graph,
                parent_run=parent_run,
                node=node,
                node_id="s1",
                input_payload={},
                step_tracker=None,
            )


# ---------------------------------------------------------------------------
# Tests: Deployment-version pin (resume after HITL pause must not drift)
# ---------------------------------------------------------------------------


def _make_versioned_deployment(graph: Graph, version: int) -> Deployment:
    """A child deployment with an explicit deployment ``version``."""
    return Deployment(
        deployment_id=f"dep-child-v{version}",
        deployment_ref="child-g",
        version=version,
        graph_id=graph.graph_id,
        graph_version=graph.version,
        graph_version_ref=f"{graph.graph_id}:v{graph.version}",
        serialized_graph=serialize_graph(graph),
    )


def _make_paused_child_run(metadata: dict) -> Run:
    """A previously-created child Run, as resume() would load from the repo."""
    return Run(
        run_id="child-run-paused",
        graph_version_ref="child-g:v1",
        deployment_ref="child-g",
        thread_id="child-thread",
        tenant_id="tenant-1",
        workspace_id="ws-1",
        status=RunStatus.RUNNING,
        metadata=metadata,
    )


class TestSubgraphExecutorDeploymentVersionPin:
    """start() records the resolved deployment version; resume() re-resolves it."""

    @pytest.mark.asyncio
    async def test_start_persists_deployment_version_in_metadata(self) -> None:
        # The resolved deployment version is pinned into child-run metadata so a
        # later resume re-resolves the SAME version, not whatever is latest then.
        child_graph = _make_child_graph()
        deployment = _make_versioned_deployment(child_graph, version=7)
        resolver = MagicMock(spec=SubgraphResolver)
        resolver.resolve = AsyncMock(return_value=(child_graph, deployment))

        executor = SubgraphExecutor(resolver=resolver)
        orch = _make_orchestrator()
        parent_graph = _make_parent_graph()
        parent_run = _make_parent_run()
        node = parent_graph.nodes[0]

        await executor.execute(
            orchestrator=orch,
            parent_graph=parent_graph,
            parent_run=parent_run,
            node=node,
            node_id="s1",
            input_payload={},
            step_tracker=None,
        )

        child_run_arg = orch.run_repository.create.call_args[0][0]
        assert child_run_arg.metadata["subgraph_deployment_version"] == 7

    @pytest.mark.asyncio
    async def test_resume_resolves_pinned_deployment_version(self) -> None:
        # A run paused at v1 must resume on v1 even if the ref was republished to a
        # newer version during the pause: resolve() gets version=1, not None/latest.
        child_graph = _make_child_graph()
        deployment = _make_versioned_deployment(child_graph, version=1)
        resolver = MagicMock(spec=SubgraphResolver)
        resolver.resolve = AsyncMock(return_value=(child_graph, deployment))

        executor = SubgraphExecutor(resolver=resolver)
        orch = _make_orchestrator()
        paused_child = _make_paused_child_run(
            {"subgraph_depth": 1, "subgraph_deployment_version": 1}
        )
        orch.run_repository.get = AsyncMock(return_value=paused_child)
        parent_graph = _make_parent_graph()
        parent_run = _make_parent_run()

        await executor.resume(
            orchestrator=orch,
            parent_graph=parent_graph,
            parent_run=parent_run,
            paused_child_run_id="child-run-paused",
            step_tracker=None,
        )

        # resolve(graph_ref, version, ...): the version positional arg is the pin.
        assert resolver.resolve.call_args[0][1] == 1

    @pytest.mark.asyncio
    async def test_resume_falls_back_to_latest_when_pin_absent(self) -> None:
        # Back-compat: runs paused before the pin existed have no metadata key, so
        # resume resolves with version=None (latest) — today's behavior, unchanged.
        child_graph = _make_child_graph()
        deployment = _make_versioned_deployment(child_graph, version=2)
        resolver = MagicMock(spec=SubgraphResolver)
        resolver.resolve = AsyncMock(return_value=(child_graph, deployment))

        executor = SubgraphExecutor(resolver=resolver)
        orch = _make_orchestrator()
        paused_child = _make_paused_child_run({"subgraph_depth": 1})
        orch.run_repository.get = AsyncMock(return_value=paused_child)
        parent_graph = _make_parent_graph()
        parent_run = _make_parent_run()

        await executor.resume(
            orchestrator=orch,
            parent_graph=parent_graph,
            parent_run=parent_run,
            paused_child_run_id="child-run-paused",
            step_tracker=None,
        )

        assert resolver.resolve.call_args[0][1] is None
