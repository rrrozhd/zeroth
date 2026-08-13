"""Lifetime of the per-fork branch-output buckets in ``run.metadata`` (A16-12).

``TokenRuntimeSupport._merge_closed_fanout`` accumulates each branch's output
under ``run.metadata["token_fanout_results"][fork_id]`` so the last branch to
arrive can reduce them all. That bucket has exactly one reader -- the call that
observes the fork closing -- so every path that observes a closed fork has to
drop it. These tests pin both halves: the bucket must survive while the fork is
open, and must not survive a call that sees it closed.

The method reads only duck-typed attributes off the snapshot and never touches
``self``, so it is exercised unbound against light stand-ins for the snapshot
records. The one enum that matters (``lifecycle_state.value``) is the real one.
"""

from __future__ import annotations

from types import SimpleNamespace

from zeroth.contracts.graph import (
    AgentNode,
    AgentNodeData,
    ExecutionSettings,
    ForkLifecycleState,
    Graph,
)
from zeroth.contracts.graph.models import ParallelConfig
from zeroth.runtime.orchestration.token_runtime_support import TokenRuntimeSupport
from zeroth.runtime.runs import Run

GRAPH_VERSION = "fanout-graph@1"


def _graph(*, parallel_config: ParallelConfig | None) -> Graph:
    return Graph(
        graph_id="fanout-graph",
        name="Fan-out graph",
        version=1,
        entry_step="root",
        execution_settings=ExecutionSettings(max_total_steps=5),
        nodes=[
            AgentNode(
                node_id="root",
                graph_version_ref=GRAPH_VERSION,
                agent=AgentNodeData(instruction="echo", model_provider="provider://demo"),
                parallel_config=parallel_config,
            )
        ],
        edges=[],
    )


def _run() -> Run:
    return Run(run_id="run-1", graph_version_ref=GRAPH_VERSION, deployment_ref="deploy-1")


def _token(token_id: str, fork_id: str = "fork-1") -> SimpleNamespace:
    return SimpleNamespace(
        token_id=token_id,
        fork_lineage=(SimpleNamespace(fork_id=fork_id),),
    )


def _snapshot(
    *,
    lifecycle: ForkLifecycleState,
    children: tuple[str, ...],
    fork_id: str = "fork-1",
) -> SimpleNamespace:
    return SimpleNamespace(
        tokens=(SimpleNamespace(token_id="parent", current_node_id="root"),),
        forks=(
            SimpleNamespace(
                fork_id=fork_id,
                lifecycle_state=lifecycle,
                parent_token_id="parent",
                children=tuple(SimpleNamespace(token_id=child) for child in children),
            ),
        ),
    )


def _merge(graph: Graph, run: Run, token, output, after) -> dict:
    before = SimpleNamespace(tokens=after.tokens)
    return TokenRuntimeSupport._merge_closed_fanout(None, graph, run, before, token, output, after)


def test_an_open_fork_keeps_accumulating_its_branch_outputs() -> None:
    """The bucket is the only record of branches that already reported.

    This is why the write cannot simply move below the early returns: the
    not-yet-closed path is precisely the one that has to persist it.
    """
    graph = _graph(parallel_config=ParallelConfig(split_path="items"))
    run = _run()
    after = _snapshot(lifecycle=ForkLifecycleState.OPEN, children=("t1", "t2"))

    _merge(graph, run, _token("t1"), {"value": 1}, after)
    _merge(graph, run, _token("t2"), {"value": 2}, after)

    assert run.metadata["token_fanout_results"] == {
        "fork-1": {"t1": {"value": 1}, "t2": {"value": 2}}
    }


def test_a_closed_fork_that_merges_leaves_no_bucket_behind() -> None:
    """The reducing path drops the key entirely rather than leaving ``{}``."""
    graph = _graph(parallel_config=ParallelConfig(split_path="items"))
    run = _run()
    open_fork = _snapshot(lifecycle=ForkLifecycleState.OPEN, children=("t1", "t2"))
    closed_fork = _snapshot(lifecycle=ForkLifecycleState.CLOSED, children=("t1", "t2"))

    _merge(graph, run, _token("t1"), {"value": 1}, open_fork)
    merged = _merge(graph, run, _token("t2"), {"value": 2}, closed_fork)

    assert merged == {"items": [{"value": 1}, {"value": 2}]}
    assert "token_fanout_results" not in run.metadata


def test_a_closed_fork_whose_owner_has_no_parallel_config_leaks_no_bucket() -> None:
    """A16-12: the defensive return must not keep the bucket alive forever.

    A fork whose owning node reports no ``parallel_config`` returns the branch
    output unchanged -- but the fork is closed, so nothing will ever call this
    again for that fork id, and the bucket written on the way in has no reader
    left. It used to stay in ``run.metadata`` for the life of the run and be
    persisted with it.
    """
    graph = _graph(parallel_config=None)
    run = _run()
    after = _snapshot(lifecycle=ForkLifecycleState.CLOSED, children=("t1",))

    output = _merge(graph, run, _token("t1"), {"value": 1}, after)

    assert output == {"value": 1}
    assert "token_fanout_results" not in run.metadata, (
        "a closed fork's bucket outlives its only reader"
    )


def test_a_sibling_forks_bucket_survives_another_fork_closing() -> None:
    """Cleanup is keyed on the fork that closed, not on the whole map."""
    graph = _graph(parallel_config=None)
    run = _run()
    run.metadata["token_fanout_results"] = {"fork-2": {"other": {"value": 9}}}
    after = _snapshot(lifecycle=ForkLifecycleState.CLOSED, children=("t1",))

    _merge(graph, run, _token("t1"), {"value": 1}, after)

    assert run.metadata["token_fanout_results"] == {"fork-2": {"other": {"value": 9}}}
