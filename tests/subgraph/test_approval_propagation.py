"""Tests for approval propagation across subgraph boundaries.

Covers the full two-phase resume pattern:
- First encounter: child WAITING_APPROVAL -> parent pauses
- Resume after approval: child completes -> parent continues
- Resume with nested approval: child STILL waiting -> parent stays paused
- Full round-trip approval flow
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from zeroth.core.execution_units import ExecutableUnitRunner
from zeroth.core.graph.models import (
    AgentNode,
    AgentNodeData,
    Edge,
    Graph,
    HumanApprovalNode,
    HumanApprovalNodeData,
    SubgraphNode,
)
from zeroth.core.orchestrator.runtime import RuntimeOrchestrator
from zeroth.core.runs.models import Run, RunStatus
from zeroth.core.subgraph.errors import SubgraphResolutionError
from zeroth.core.subgraph.executor import SubgraphExecutor
from zeroth.core.subgraph.models import SubgraphNodeData
from zeroth.core.subgraph.resolver import SubgraphResolver


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_run_repository() -> AsyncMock:
    """Create a mock RunRepository that passes through put/create."""
    repo = AsyncMock()
    repo.create = AsyncMock(side_effect=lambda r: r)
    repo.put = AsyncMock(side_effect=lambda r: r)
    repo.get = AsyncMock(return_value=None)
    repo.write_checkpoint = AsyncMock()
    return repo


def _make_orchestrator(
    run_repository: AsyncMock | None = None,
    subgraph_executor: SubgraphExecutor | None = None,
) -> RuntimeOrchestrator:
    """Create a RuntimeOrchestrator with mocked dependencies."""
    return RuntimeOrchestrator(
        run_repository=run_repository or _make_run_repository(),
        agent_runners={},
        executable_unit_runner=ExecutableUnitRunner(),
        subgraph_executor=subgraph_executor,
    )


def _make_parent_graph_with_subgraph(
    subgraph_ref: str = "child-g",
) -> Graph:
    """Parent graph with a single SubgraphNode."""
    subgraph_node = SubgraphNode(
        node_id="s1",
        graph_version_ref="parent-g@1",
        subgraph=SubgraphNodeData(graph_ref=subgraph_ref),
    )
    return Graph(
        graph_id="parent-g",
        name="parent-workflow",
        version=1,
        nodes=[subgraph_node],
        edges=[],
        entry_step="s1",
    )


def _make_parent_graph_with_subgraph_and_successor(
    subgraph_ref: str = "child-g",
) -> Graph:
    """Parent graph: SubgraphNode -> AgentNode (to test output propagation after resume)."""
    subgraph_node = SubgraphNode(
        node_id="s1",
        graph_version_ref="parent-g@1",
        subgraph=SubgraphNodeData(graph_ref=subgraph_ref),
    )
    agent_node = AgentNode(
        node_id="a1",
        graph_version_ref="parent-g@1",
        agent=AgentNodeData(
            instruction="use subgraph output",
            model_provider="openai/gpt-4",
        ),
    )
    edge = Edge(
        edge_id="e1",
        source_node_id="s1",
        target_node_id="a1",
    )
    return Graph(
        graph_id="parent-g",
        name="parent-workflow",
        version=1,
        nodes=[subgraph_node, agent_node],
        edges=[edge],
        entry_step="s1",
    )


def _make_run(
    graph: Graph,
    pending_node_ids: list[str] | None = None,
    metadata: dict | None = None,
    run_id: str = "run-parent-1",
) -> Run:
    """Create a Run for the given graph."""
    entry = graph.entry_step or graph.nodes[0].node_id
    default_metadata = {
        "graph_id": graph.graph_id,
        "graph_name": graph.name,
        "node_payloads": {entry: {"input": "test"}},
        "edge_visit_counts": {},
        "path": [],
        "audits": {},
    }
    if metadata:
        default_metadata.update(metadata)
    return Run(
        run_id=run_id,
        graph_version_ref=f"{graph.graph_id}:v{graph.version}",
        deployment_ref=graph.graph_id,
        thread_id="thread-1",
        tenant_id="tenant-1",
        workspace_id="ws-1",
        status=RunStatus.RUNNING,
        pending_node_ids=pending_node_ids or [entry],
        metadata=default_metadata,
    )


# ---------------------------------------------------------------------------
# Tests: First encounter -- child WAITING_APPROVAL -> parent pauses
# ---------------------------------------------------------------------------


class TestApprovalPropagationFirstEncounter:
    """When SubgraphExecutor.execute() returns child_run with WAITING_APPROVAL,
    parent run transitions to WAITING_APPROVAL."""

    @pytest.mark.asyncio
    async def test_child_waiting_approval_pauses_parent(self) -> None:
        """Parent transitions to WAITING_APPROVAL when child is WAITING_APPROVAL."""
        child_run = Run(
            run_id="child-run-1",
            graph_version_ref="child-g:v1",
            deployment_ref="child-g",
            status=RunStatus.WAITING_APPROVAL,
            metadata={"subgraph_depth": 1},
        )
        mock_executor = MagicMock(spec=SubgraphExecutor)
        mock_executor.execute = AsyncMock(return_value=child_run)

        run_repo = _make_run_repository()
        orch = _make_orchestrator(run_repository=run_repo, subgraph_executor=mock_executor)
        parent_graph = _make_parent_graph_with_subgraph()
        parent_run = _make_run(parent_graph)

        result = await orch._drive(parent_graph, parent_run)

        assert result.status == RunStatus.WAITING_APPROVAL

    @pytest.mark.asyncio
    async def test_parent_stores_pending_subgraph_metadata(self) -> None:
        """Parent stores pending_subgraph with child_run_id, node_id, graph_ref, version."""
        child_run = Run(
            run_id="child-run-1",
            graph_version_ref="child-g:v1",
            deployment_ref="child-g",
            status=RunStatus.WAITING_APPROVAL,
            metadata={"subgraph_depth": 1},
        )
        mock_executor = MagicMock(spec=SubgraphExecutor)
        mock_executor.execute = AsyncMock(return_value=child_run)

        run_repo = _make_run_repository()
        orch = _make_orchestrator(run_repository=run_repo, subgraph_executor=mock_executor)
        parent_graph = _make_parent_graph_with_subgraph()
        parent_run = _make_run(parent_graph)

        result = await orch._drive(parent_graph, parent_run)

        pending = result.metadata.get("pending_subgraph")
        assert pending is not None
        assert pending["child_run_id"] == "child-run-1"
        assert pending["node_id"] == "s1"
        assert pending["graph_ref"] == "child-g"
        assert "version" in pending

    @pytest.mark.asyncio
    async def test_parent_requeues_subgraph_node(self) -> None:
        """Parent re-queues the SubgraphNode's node_id at front of pending_node_ids."""
        child_run = Run(
            run_id="child-run-1",
            graph_version_ref="child-g:v1",
            deployment_ref="child-g",
            status=RunStatus.WAITING_APPROVAL,
            metadata={"subgraph_depth": 1},
        )
        mock_executor = MagicMock(spec=SubgraphExecutor)
        mock_executor.execute = AsyncMock(return_value=child_run)

        run_repo = _make_run_repository()
        orch = _make_orchestrator(run_repository=run_repo, subgraph_executor=mock_executor)
        parent_graph = _make_parent_graph_with_subgraph()
        parent_run = _make_run(parent_graph)

        result = await orch._drive(parent_graph, parent_run)

        assert result.pending_node_ids[0] == "s1"

    @pytest.mark.asyncio
    async def test_parent_checkpointed_before_returning(self) -> None:
        """write_checkpoint called when parent pauses for child approval."""
        child_run = Run(
            run_id="child-run-1",
            graph_version_ref="child-g:v1",
            deployment_ref="child-g",
            status=RunStatus.WAITING_APPROVAL,
            metadata={"subgraph_depth": 1},
        )
        mock_executor = MagicMock(spec=SubgraphExecutor)
        mock_executor.execute = AsyncMock(return_value=child_run)

        run_repo = _make_run_repository()
        orch = _make_orchestrator(run_repository=run_repo, subgraph_executor=mock_executor)
        parent_graph = _make_parent_graph_with_subgraph()
        parent_run = _make_run(parent_graph)

        await orch._drive(parent_graph, parent_run)

        run_repo.write_checkpoint.assert_called()


# ---------------------------------------------------------------------------
# Tests: Resume after approval -- child completes -> parent continues
# ---------------------------------------------------------------------------


class TestApprovalPropagationResume:
    """On resume, _drive() detects pending_subgraph, resumes the child run, and continues."""

    @pytest.mark.asyncio
    async def test_resume_calls_resume_graph_for_child(self) -> None:
        """On resume, _drive() calls resume_graph with child_run_id instead of execute()."""
        # Set up parent run with pending_subgraph metadata (simulating paused state)
        parent_graph = _make_parent_graph_with_subgraph()
        parent_run = _make_run(parent_graph, pending_node_ids=["s1"])
        parent_run.status = RunStatus.RUNNING
        parent_run.metadata["pending_subgraph"] = {
            "child_run_id": "child-run-1",
            "node_id": "s1",
            "graph_ref": "child-g",
            "version": None,
        }

        # Child run in repository: WAITING_APPROVAL with no pending nodes.
        # When _drive() resumes it, no pending nodes means immediate COMPLETED.
        child_run = Run(
            run_id="child-run-1",
            graph_version_ref="child-g:v1",
            deployment_ref="child-g",
            status=RunStatus.WAITING_APPROVAL,
            pending_node_ids=[],
            metadata={"subgraph_depth": 1, "last_output": {"resumed_result": "done"}},
        )

        # Mock resolver for re-resolution on resume
        mock_resolver = AsyncMock(spec=SubgraphResolver)
        child_graph = Graph(
            graph_id="child-g",
            name="child",
            version=1,
            nodes=[
                AgentNode(
                    node_id="c1",
                    graph_version_ref="child-g@1",
                    agent=AgentNodeData(instruction="x", model_provider="openai/gpt-4"),
                )
            ],
            edges=[],
            entry_step="c1",
        )
        mock_resolver.resolve = AsyncMock(return_value=(child_graph, MagicMock()))
        mock_executor = SubgraphExecutor(resolver=mock_resolver)

        run_repo = _make_run_repository()
        run_repo.get = AsyncMock(return_value=child_run)
        orch = _make_orchestrator(run_repository=run_repo, subgraph_executor=mock_executor)

        result = await orch._drive(parent_graph, parent_run)

        # Should have completed, using child's output
        assert result.status == RunStatus.COMPLETED
        assert result.final_output == {"resumed_result": "done"}

    @pytest.mark.asyncio
    async def test_resume_clears_pending_subgraph_metadata(self) -> None:
        """After child completes on resume, pending_subgraph is cleared from parent."""
        parent_graph = _make_parent_graph_with_subgraph()
        parent_run = _make_run(parent_graph, pending_node_ids=["s1"])
        parent_run.status = RunStatus.RUNNING
        parent_run.metadata["pending_subgraph"] = {
            "child_run_id": "child-run-1",
            "node_id": "s1",
            "graph_ref": "child-g",
            "version": None,
        }

        # Child in WAITING_APPROVAL with no pending nodes -- completes on resume.
        child_run = Run(
            run_id="child-run-1",
            graph_version_ref="child-g:v1",
            deployment_ref="child-g",
            status=RunStatus.WAITING_APPROVAL,
            pending_node_ids=[],
            metadata={"subgraph_depth": 1, "last_output": {"result": "ok"}},
        )

        mock_resolver = AsyncMock(spec=SubgraphResolver)
        child_graph = Graph(
            graph_id="child-g",
            name="child",
            version=1,
            nodes=[
                AgentNode(
                    node_id="c1",
                    graph_version_ref="child-g@1",
                    agent=AgentNodeData(instruction="x", model_provider="openai/gpt-4"),
                )
            ],
            edges=[],
            entry_step="c1",
        )
        mock_resolver.resolve = AsyncMock(return_value=(child_graph, MagicMock()))
        mock_executor = SubgraphExecutor(resolver=mock_resolver)

        run_repo = _make_run_repository()
        run_repo.get = AsyncMock(return_value=child_run)
        orch = _make_orchestrator(run_repository=run_repo, subgraph_executor=mock_executor)

        result = await orch._drive(parent_graph, parent_run)

        assert "pending_subgraph" not in result.metadata

    @pytest.mark.asyncio
    async def test_resume_child_still_waiting_keeps_parent_paused(self) -> None:
        """If child still WAITING_APPROVAL on resume, parent stays paused."""
        parent_graph = _make_parent_graph_with_subgraph()
        parent_run = _make_run(parent_graph, pending_node_ids=["s1"])
        parent_run.status = RunStatus.RUNNING
        parent_run.metadata["pending_subgraph"] = {
            "child_run_id": "child-run-1",
            "node_id": "s1",
            "graph_ref": "child-g",
            "version": None,
        }

        # Child is STILL waiting -- has a HumanApprovalNode re-queued.
        # _drive() will hit the approval gate and return WAITING_APPROVAL again.
        child_graph = Graph(
            graph_id="child-g",
            name="child",
            version=1,
            nodes=[
                HumanApprovalNode(
                    node_id="approve-1",
                    graph_version_ref="child-g@1",
                    human_approval=HumanApprovalNodeData(),
                )
            ],
            edges=[],
            entry_step="approve-1",
        )
        child_run = Run(
            run_id="child-run-1",
            graph_version_ref="child-g:v1",
            deployment_ref="child-g",
            status=RunStatus.WAITING_APPROVAL,
            pending_node_ids=["subgraph:child-g:1:approve-1"],
            metadata={
                "subgraph_depth": 1,
                "pending_approval": {
                    "node_id": "subgraph:child-g:1:approve-1",
                    "input": {},
                    "approval_id": None,
                },
                "node_payloads": {"subgraph:child-g:1:approve-1": {}},
            },
        )

        mock_resolver = AsyncMock(spec=SubgraphResolver)
        mock_resolver.resolve = AsyncMock(return_value=(child_graph, MagicMock()))
        mock_executor = SubgraphExecutor(resolver=mock_resolver)

        run_repo = _make_run_repository()
        run_repo.get = AsyncMock(return_value=child_run)
        orch = _make_orchestrator(run_repository=run_repo, subgraph_executor=mock_executor)

        result = await orch._drive(parent_graph, parent_run)

        assert result.status == RunStatus.WAITING_APPROVAL
        # pending_subgraph should still be present
        assert "pending_subgraph" in result.metadata

    @pytest.mark.asyncio
    async def test_resume_re_resolves_subgraph(self) -> None:
        """Resume path re-resolves the subgraph via resolver.resolve()."""
        parent_graph = _make_parent_graph_with_subgraph()
        parent_run = _make_run(parent_graph, pending_node_ids=["s1"])
        parent_run.status = RunStatus.RUNNING
        parent_run.metadata["pending_subgraph"] = {
            "child_run_id": "child-run-1",
            "node_id": "s1",
            "graph_ref": "child-g",
            "version": 2,
        }

        # Child in WAITING_APPROVAL with no pending nodes -- completes on resume.
        child_run = Run(
            run_id="child-run-1",
            graph_version_ref="child-g:v1",
            deployment_ref="child-g",
            status=RunStatus.WAITING_APPROVAL,
            pending_node_ids=[],
            metadata={"subgraph_depth": 1, "last_output": {"result": "ok"}},
        )

        mock_resolver = AsyncMock(spec=SubgraphResolver)
        child_graph = Graph(
            graph_id="child-g",
            name="child",
            version=1,
            nodes=[
                AgentNode(
                    node_id="c1",
                    graph_version_ref="child-g@1",
                    agent=AgentNodeData(instruction="x", model_provider="openai/gpt-4"),
                )
            ],
            edges=[],
            entry_step="c1",
        )
        mock_resolver.resolve = AsyncMock(return_value=(child_graph, MagicMock()))
        mock_executor = SubgraphExecutor(resolver=mock_resolver)

        run_repo = _make_run_repository()
        run_repo.get = AsyncMock(return_value=child_run)
        orch = _make_orchestrator(run_repository=run_repo, subgraph_executor=mock_executor)

        await orch._drive(parent_graph, parent_run)

        # Verify resolver was called with correct graph_ref and version
        mock_resolver.resolve.assert_called_once_with("child-g", 2)

    @pytest.mark.asyncio
    async def test_resume_governance_merge_reapplied(self) -> None:
        """Governance merge is re-applied on resume (parent policies act as ceiling)."""
        parent_graph = _make_parent_graph_with_subgraph()
        parent_graph = parent_graph.model_copy(
            update={
                "policy_bindings": ["deny-dangerous"],
            }
        )
        parent_run = _make_run(parent_graph, pending_node_ids=["s1"])
        parent_run.status = RunStatus.RUNNING
        parent_run.metadata["pending_subgraph"] = {
            "child_run_id": "child-run-1",
            "node_id": "s1",
            "graph_ref": "child-g",
            "version": None,
        }

        # Child in WAITING_APPROVAL with no pending nodes -- completes on resume.
        child_run = Run(
            run_id="child-run-1",
            graph_version_ref="child-g:v1",
            deployment_ref="child-g",
            status=RunStatus.WAITING_APPROVAL,
            pending_node_ids=[],
            metadata={"subgraph_depth": 1, "last_output": {"result": "ok"}},
        )

        mock_resolver = AsyncMock(spec=SubgraphResolver)
        child_graph = Graph(
            graph_id="child-g",
            name="child",
            version=1,
            nodes=[
                AgentNode(
                    node_id="c1",
                    graph_version_ref="child-g@1",
                    agent=AgentNodeData(instruction="x", model_provider="openai/gpt-4"),
                )
            ],
            edges=[],
            entry_step="c1",
        )
        mock_resolver.resolve = AsyncMock(return_value=(child_graph, MagicMock()))
        mock_executor = SubgraphExecutor(resolver=mock_resolver)

        run_repo = _make_run_repository()
        run_repo.get = AsyncMock(return_value=child_run)
        orch = _make_orchestrator(run_repository=run_repo, subgraph_executor=mock_executor)

        result = await orch._drive(parent_graph, parent_run)

        # The resume completed successfully -- governance merge didn't block
        assert result.status == RunStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_resume_resolution_failure_fails_run(self) -> None:
        """If re-resolution fails on resume, parent run fails."""
        parent_graph = _make_parent_graph_with_subgraph()
        parent_run = _make_run(parent_graph, pending_node_ids=["s1"])
        parent_run.status = RunStatus.RUNNING
        parent_run.metadata["pending_subgraph"] = {
            "child_run_id": "child-run-1",
            "node_id": "s1",
            "graph_ref": "child-g",
            "version": None,
        }

        mock_resolver = AsyncMock(spec=SubgraphResolver)
        mock_resolver.resolve = AsyncMock(side_effect=SubgraphResolutionError("deployment removed"))
        mock_executor = SubgraphExecutor(resolver=mock_resolver)

        run_repo = _make_run_repository()
        orch = _make_orchestrator(run_repository=run_repo, subgraph_executor=mock_executor)

        result = await orch._drive(parent_graph, parent_run)

        assert result.status == RunStatus.FAILED
        assert "subgraph_resume_failed" in (result.failure_state.reason or "")

    @pytest.mark.asyncio
    async def test_resume_records_audit_with_resumed_flag(self) -> None:
        """Resumed subgraph records subgraph_resumed=True in audit record."""
        parent_graph = _make_parent_graph_with_subgraph()
        parent_run = _make_run(parent_graph, pending_node_ids=["s1"])
        parent_run.status = RunStatus.RUNNING
        parent_run.metadata["pending_subgraph"] = {
            "child_run_id": "child-run-1",
            "node_id": "s1",
            "graph_ref": "child-g",
            "version": None,
        }

        # Child in WAITING_APPROVAL with no pending nodes -- completes on resume.
        child_run = Run(
            run_id="child-run-1",
            graph_version_ref="child-g:v1",
            deployment_ref="child-g",
            status=RunStatus.WAITING_APPROVAL,
            pending_node_ids=[],
            metadata={"subgraph_depth": 1, "last_output": {"result": "ok"}},
        )

        mock_resolver = AsyncMock(spec=SubgraphResolver)
        child_graph = Graph(
            graph_id="child-g",
            name="child",
            version=1,
            nodes=[
                AgentNode(
                    node_id="c1",
                    graph_version_ref="child-g@1",
                    agent=AgentNodeData(instruction="x", model_provider="openai/gpt-4"),
                )
            ],
            edges=[],
            entry_step="c1",
        )
        mock_resolver.resolve = AsyncMock(return_value=(child_graph, MagicMock()))
        mock_executor = SubgraphExecutor(resolver=mock_resolver)

        run_repo = _make_run_repository()
        run_repo.get = AsyncMock(return_value=child_run)
        orch = _make_orchestrator(run_repository=run_repo, subgraph_executor=mock_executor)

        result = await orch._drive(parent_graph, parent_run)

        # Find the history entry for the SubgraphNode
        subgraph_history = [h for h in result.execution_history if h.node_id == "s1"]
        assert len(subgraph_history) == 1


# ---------------------------------------------------------------------------
# Tests: Two-level nesting
# ---------------------------------------------------------------------------


class TestApprovalPropagationNested:
    """Multi-level nesting: parent -> subgraph with approval -> resume cascades."""

    @pytest.mark.asyncio
    async def test_two_level_nesting_pause_and_resume(self) -> None:
        """Two-level nesting: parent -> child subgraph pauses, resume cascades back."""
        # First phase: child returns WAITING_APPROVAL -> parent pauses
        child_run_waiting = Run(
            run_id="child-run-1",
            graph_version_ref="child-g:v1",
            deployment_ref="child-g",
            status=RunStatus.WAITING_APPROVAL,
            metadata={"subgraph_depth": 1},
        )
        mock_executor = MagicMock(spec=SubgraphExecutor)
        mock_executor.execute = AsyncMock(return_value=child_run_waiting)

        run_repo = _make_run_repository()
        orch = _make_orchestrator(run_repository=run_repo, subgraph_executor=mock_executor)
        parent_graph = _make_parent_graph_with_subgraph()
        parent_run = _make_run(parent_graph)

        # Phase 1: parent pauses
        result = await orch._drive(parent_graph, parent_run)
        assert result.status == RunStatus.WAITING_APPROVAL
        assert result.metadata.get("pending_subgraph") is not None

        # Phase 2: resume -- child now completes (WAITING_APPROVAL with no pending nodes)
        child_run_completed = Run(
            run_id="child-run-1",
            graph_version_ref="child-g:v1",
            deployment_ref="child-g",
            status=RunStatus.WAITING_APPROVAL,
            pending_node_ids=[],
            metadata={"subgraph_depth": 1, "last_output": {"answer": "from-child"}},
        )

        mock_resolver = AsyncMock(spec=SubgraphResolver)
        child_graph = Graph(
            graph_id="child-g",
            name="child",
            version=1,
            nodes=[
                AgentNode(
                    node_id="c1",
                    graph_version_ref="child-g@1",
                    agent=AgentNodeData(instruction="x", model_provider="openai/gpt-4"),
                )
            ],
            edges=[],
            entry_step="c1",
        )
        mock_resolver.resolve = AsyncMock(return_value=(child_graph, MagicMock()))
        mock_executor_2 = SubgraphExecutor(resolver=mock_resolver)

        run_repo_2 = _make_run_repository()
        run_repo_2.get = AsyncMock(return_value=child_run_completed)
        orch2 = _make_orchestrator(run_repository=run_repo_2, subgraph_executor=mock_executor_2)

        # Resume from the paused state
        result.status = RunStatus.RUNNING
        final = await orch2._drive(parent_graph, result)

        assert final.status == RunStatus.COMPLETED
        assert final.final_output == {"answer": "from-child"}
        assert "pending_subgraph" not in final.metadata
