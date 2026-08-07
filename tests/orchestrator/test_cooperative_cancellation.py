"""Audit F3: an operator's out-of-band cancel/interrupt must actually stop an.

in-flight run before the next node dispatches.

The drive loop holds an in-memory Run and blind-writes RUNNING every node hop.
Previously an admin cancel (-> FAILED) or interrupt (-> WAITING_INTERRUPT) written
mid-dispatch was clobbered by the loop's next RUNNING write, so the run drove to
completion. These tests drive the real loop and flip the persisted status while a
node is dispatching, asserting the loop observes it and halts.
"""

from __future__ import annotations

from typing import Any

import pytest

from zeroth.contracts.graph import AgentNode, AgentNodeData, Edge, Graph
from zeroth.governance.audit import AuditRepository
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.runtime.orchestration import RuntimeOrchestrator
from zeroth.runtime.runs import Run, RunFailureState, RunStatus


class _CancelDuringDispatchRunner:
    """A runner that flips the persisted run status mid-dispatch, as an operator's.

    admin cancel/interrupt would from another task.
    """

    def __init__(self, repo: RunRepository, run_id: str, *, transition_to: RunStatus) -> None:
        self._repo = repo
        self._run_id = run_id
        self._to = transition_to
        self.call_count = 0
        self.memory_resolver = None
        self.budget_enforcer = None

    async def run(
        self,
        input_payload: Any,
        *,
        thread_id: str | None = None,
        runtime_context: Any = None,
        enforcement_context: Any = None,
    ) -> Any:
        self.call_count += 1
        if self._to is RunStatus.FAILED:
            await self._repo.transition(
                self._run_id,
                RunStatus.FAILED,
                failure_state=RunFailureState(
                    reason="operator_cancelled", message="cancelled by admin"
                ),
            )
        else:
            await self._repo.transition(self._run_id, self._to)

        class _Result:
            output_data = {"answer": "ok"}
            audit_record: dict[str, Any] = {}

        return _Result()


class _PlainRunner:
    """A runner that must NOT be reached once the run is stopped."""

    def __init__(self) -> None:
        self.call_count = 0
        self.memory_resolver = None
        self.budget_enforcer = None

    async def run(
        self,
        input_payload: Any,
        *,
        thread_id: str | None = None,
        runtime_context: Any = None,
        enforcement_context: Any = None,
    ) -> Any:
        self.call_count += 1

        class _Result:
            output_data = {"answer": "ok"}
            audit_record: dict[str, Any] = {}

        return _Result()


def _linear_graph(node_ids: list[str]) -> Graph:
    nodes = [
        AgentNode(
            node_id=nid,
            graph_version_ref="cancel-graph:v1",
            agent=AgentNodeData(instruction="test", model_provider="provider://test"),
        )
        for nid in node_ids
    ]
    edges = [
        Edge(edge_id=f"edge-{i}", source_node_id=src, target_node_id=dst)
        for i, (src, dst) in enumerate(zip(node_ids, node_ids[1:], strict=False))
    ]
    return Graph(
        graph_id="cancel-graph",
        name="cancel",
        version=1,
        entry_step=node_ids[0],
        nodes=nodes,
        edges=edges,
    )


async def _seed_running(repo: RunRepository, node_ids: list[str]) -> Run:
    run = Run(
        graph_version_ref="cancel-graph@1",
        deployment_ref="dep",
        thread_id="",
        current_node_ids=[],
        pending_node_ids=list(node_ids),
        metadata={},
    )
    persisted = await repo.create(run)
    persisted.status = RunStatus.RUNNING
    persisted.touch()
    persisted = await repo.put(persisted)
    await repo.write_checkpoint(persisted)
    return persisted


def _orchestrator(sqlite_db, runners: dict[str, Any]) -> RuntimeOrchestrator:
    return RuntimeOrchestrator(
        run_repository=RunRepository(sqlite_db),
        audit_repository=AuditRepository(sqlite_db),
        agent_runners=runners,
        executable_unit_runner=None,
        budget_enforcer=None,
    )


@pytest.mark.asyncio
async def test_operator_cancel_mid_dispatch_stops_before_next_node(sqlite_db) -> None:
    repo = RunRepository(sqlite_db)
    persisted = await _seed_running(repo, ["n1", "n2"])
    n1 = _CancelDuringDispatchRunner(repo, persisted.run_id, transition_to=RunStatus.FAILED)
    n2 = _PlainRunner()
    orchestrator = _orchestrator(sqlite_db, {"n1": n1, "n2": n2})

    result = await orchestrator._drive(_linear_graph(["n1", "n2"]), persisted)

    assert result.status is RunStatus.FAILED
    assert n1.call_count == 1
    assert n2.call_count == 0  # cancel observed before n2 dispatched


@pytest.mark.asyncio
async def test_operator_interrupt_mid_dispatch_stops_before_next_node(sqlite_db) -> None:
    repo = RunRepository(sqlite_db)
    persisted = await _seed_running(repo, ["n1", "n2"])
    n1 = _CancelDuringDispatchRunner(
        repo, persisted.run_id, transition_to=RunStatus.WAITING_INTERRUPT
    )
    n2 = _PlainRunner()
    orchestrator = _orchestrator(sqlite_db, {"n1": n1, "n2": n2})

    result = await orchestrator._drive(_linear_graph(["n1", "n2"]), persisted)

    assert result.status is RunStatus.WAITING_INTERRUPT
    assert n1.call_count == 1
    assert n2.call_count == 0


@pytest.mark.asyncio
async def test_precancelled_run_never_dispatches(sqlite_db) -> None:
    """A run already cancelled before the loop starts dispatches nothing."""
    repo = RunRepository(sqlite_db)
    persisted = await _seed_running(repo, ["n1", "n2"])
    await repo.transition(
        persisted.run_id,
        RunStatus.FAILED,
        failure_state=RunFailureState(reason="operator_cancelled", message="x"),
    )
    n1 = _PlainRunner()
    orchestrator = _orchestrator(sqlite_db, {"n1": n1, "n2": _PlainRunner()})

    result = await orchestrator._drive(_linear_graph(["n1", "n2"]), persisted)

    assert result.status is RunStatus.FAILED
    assert n1.call_count == 0


@pytest.mark.asyncio
async def test_cancel_then_replay_resumes_remaining_nodes(sqlite_db) -> None:
    """F3 re-audit: a cancelled run's queued successors are persisted, so a.

    FAILED->PENDING replay resumes from where it stopped instead of being marked
    COMPLETED with the remaining nodes silently skipped.
    """
    repo = RunRepository(sqlite_db)
    # Seed only the entry node; successors are queued dynamically per hop (as real
    # runs do), so the checkpoint after the cancelled hop must include n2.
    persisted = await _seed_running(repo, ["n1"])
    n1 = _CancelDuringDispatchRunner(repo, persisted.run_id, transition_to=RunStatus.FAILED)
    n2 = _PlainRunner()
    n3 = _PlainRunner()
    orchestrator = _orchestrator(sqlite_db, {"n1": n1, "n2": n2, "n3": n3})
    graph = _linear_graph(["n1", "n2", "n3"])

    result = await orchestrator._drive(graph, persisted)
    assert result.status is RunStatus.FAILED
    assert n2.call_count == 0 and n3.call_count == 0

    # The structured successor is durable in the token snapshot; the legacy
    # pending-node projection is deliberately not an engine input.
    reloaded = await repo.get(persisted.run_id)
    assert reloaded is not None
    snapshot = await repo.get_token_snapshot(persisted.run_id)
    assert snapshot is not None
    assert [token.current_node_id for token in snapshot.queue] == ["n2"]

    # Replay: FAILED -> PENDING (admin), worker claims (-> RUNNING), drive resumes.
    await repo.transition(persisted.run_id, RunStatus.PENDING)
    await repo.transition(persisted.run_id, RunStatus.RUNNING)
    resumed = await repo.get(persisted.run_id)
    result2 = await orchestrator._drive(graph, resumed)

    assert result2.status is RunStatus.COMPLETED
    assert n2.call_count == 1 and n3.call_count == 1


@pytest.mark.asyncio
async def test_external_stop_yields_to_concurrent_operator_transition(sqlite_db) -> None:
    """F3 re-audit follow-up: if an operator replay (FAILED->PENDING) lands.

    between _external_stop's read and its write, the loop must NOT blind-write the
    stale FAILED back and silently revert the operator — it yields to them.
    """
    from unittest.mock import AsyncMock

    repo = RunRepository(sqlite_db)
    persisted = await _seed_running(repo, ["n1", "n2"])
    await repo.transition(
        persisted.run_id,
        RunStatus.FAILED,
        failure_state=RunFailureState(reason="op_cancel", message="x"),
    )
    orchestrator = _orchestrator(sqlite_db, {"n1": _PlainRunner()})

    failed = await repo.get(persisted.run_id)
    assert failed is not None and failed.status is RunStatus.FAILED
    replayed = failed.model_copy(update={"status": RunStatus.PENDING})

    # get() returns FAILED (fresh) then PENDING (operator replay has landed).
    orchestrator.run_repository.get = AsyncMock(side_effect=[failed, replayed])
    put_spy = AsyncMock(side_effect=lambda r: r)
    orchestrator.run_repository.put = put_spy

    in_memory = failed.model_copy(update={"status": RunStatus.RUNNING})
    result = await orchestrator._driver.external_stop(in_memory)

    assert result is not None
    assert result.status is RunStatus.PENDING  # yielded to the operator's replay
    put_spy.assert_not_called()  # did NOT clobber back to FAILED


@pytest.mark.asyncio
async def test_external_stop_persists_when_no_concurrent_transition(sqlite_db) -> None:
    """No operator raced in: _external_stop persists the adopted stop status and.

    checkpoints the queue state (unchanged happy path).
    """
    from unittest.mock import AsyncMock

    repo = RunRepository(sqlite_db)
    persisted = await _seed_running(repo, ["n1", "n2"])
    await repo.transition(
        persisted.run_id,
        RunStatus.FAILED,
        failure_state=RunFailureState(reason="op_cancel", message="x"),
    )
    orchestrator = _orchestrator(sqlite_db, {"n1": _PlainRunner()})

    failed = await repo.get(persisted.run_id)
    assert failed is not None

    orchestrator.run_repository.get = AsyncMock(side_effect=[failed, failed])
    put_spy = AsyncMock(side_effect=lambda r: r)
    orchestrator.run_repository.put = put_spy
    wc_spy = AsyncMock()
    orchestrator.run_repository.write_checkpoint = wc_spy

    in_memory = failed.model_copy(update={"status": RunStatus.RUNNING})
    result = await orchestrator._driver.external_stop(in_memory)

    assert result is not None
    assert result.status is RunStatus.FAILED
    put_spy.assert_called_once()
    wc_spy.assert_called_once()
