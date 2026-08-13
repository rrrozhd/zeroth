"""A subgraph's step budget is never inherited by accident (ZER-48 / A06-17).

``dispatch_subgraph_node`` had no ``step_tracker`` parameter at all, so it could
not forward one even in principle; both inner calls landed on
``SubgraphExecutor``'s permissive ``step_tracker: GlobalStepTracker | None =
None`` default and a nested subgraph got a fresh budget instead of consuming the
parent's.  The legacy driver threaded the value; the token path did not.

The audit ledger's rule for this defect shape is to *delete the permissive
default* rather than only pass the value at the one call site that was noticed —
otherwise the next new caller silently reintroduces it.  These tests pin the
absence of the default, which is the part that cannot regress quietly.
"""

from __future__ import annotations

import inspect

import pytest

from zeroth.runtime.orchestration.dispatcher import dispatch_subgraph_node
from zeroth.runtime.subgraphs.executor import SubgraphExecutor


@pytest.mark.parametrize("method_name", ["execute", "resume"])
def test_subgraph_executor_has_no_default_step_tracker(method_name: str) -> None:
    parameter = inspect.signature(getattr(SubgraphExecutor, method_name)).parameters["step_tracker"]

    assert parameter.default is inspect.Parameter.empty, (
        f"SubgraphExecutor.{method_name} still defaults step_tracker, so a caller "
        "that forgets it gets a fresh step budget instead of an error"
    )
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


def test_dispatch_subgraph_node_requires_a_step_tracker() -> None:
    parameter = inspect.signature(dispatch_subgraph_node).parameters["step_tracker"]

    assert parameter.default is inspect.Parameter.empty
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


@pytest.mark.asyncio
async def test_dispatch_forwards_the_tracker_it_was_given() -> None:
    """Requiring the parameter is worthless if the value is then dropped."""
    from zeroth.contracts.governed import RunStatus

    seen: dict[str, object] = {}
    sentinel = object()

    class _Child:
        status = RunStatus.COMPLETED
        run_id = "child-1"
        final_output: dict[str, object] = {"answer": 42}
        failure_state = None
        # Read by the cost rollup the dispatcher gained from main. Empty
        # metadata sends it down the execution-history path, which an empty
        # history answers with an unmeasured rollup -- this test is about what
        # reached the executor, not about cost.
        metadata: dict[str, object] = {}
        execution_history: list[object] = []

    class _Executor:
        async def execute(self, **kwargs: object) -> _Child:
            seen.update(kwargs)
            return _Child()

    class _Subgraph:
        graph_ref = "child"
        version = "1"

    class _Node:
        node_id = "s1"
        subgraph = _Subgraph()

    class _Run:
        metadata: dict[str, object] = {}
        run_id = "parent-1"

    result = await dispatch_subgraph_node(
        executor=_Executor(),
        orchestrator=object(),
        parent_graph=object(),  # type: ignore[arg-type]
        parent_run=_Run(),  # type: ignore[arg-type]
        node=_Node(),  # type: ignore[arg-type]
        input_payload={},
        step_tracker=sentinel,
    )

    assert result.output == {"answer": 42}
    assert seen.get("step_tracker") is sentinel, (
        "dispatch_subgraph_node accepted a step tracker and did not forward it"
    )
