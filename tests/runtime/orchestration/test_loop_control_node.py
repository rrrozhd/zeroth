from __future__ import annotations

import pytest
from pydantic import ValidationError

from zeroth.contracts.graph import LoopNode, LoopNodeData
from zeroth.runtime.orchestration import NodeDispatcher, RuntimeToolExecutor
from zeroth.runtime.runs import Run


class _UnusedRunner:
    pass


def _dispatcher() -> NodeDispatcher:
    runner = _UnusedRunner()
    return NodeDispatcher(
        agent_runners={},
        executable_unit_runner=runner,
        tool_executor=RuntimeToolExecutor(executable_unit_runner=runner),
    )


def _run(*, visits: int = 0) -> Run:
    return Run(
        graph_version_ref="quality:v1",
        deployment_ref="quality-v1",
        node_visit_counts={"quality-loop": visits},
    )


def _node(*, max_retries: int = 3) -> LoopNode:
    return LoopNode(
        node_id="quality-loop",
        graph_version_ref="quality:v1",
        loop=LoopNodeData(
            until="payload.needs_repair != True",
            max_retries=max_retries,
        ),
    )


def test_loop_node_rejects_negative_max_retries() -> None:
    with pytest.raises(ValidationError):
        _node(max_retries=-1)


def test_loop_node_allows_an_incomplete_draft() -> None:
    draft = LoopNodeData(until="", max_retries=3)

    assert draft.until == ""


@pytest.mark.asyncio
async def test_loop_dispatch_refuses_an_incomplete_draft() -> None:
    node = LoopNode(
        node_id="quality-loop",
        graph_version_ref="quality:v1",
        loop=LoopNodeData(until="", max_retries=3),
    )

    with pytest.raises(Exception, match="Loop condition is required"):
        await _dispatcher().dispatch_inner(node, _run(visits=0), {})


@pytest.mark.asyncio
async def test_loop_node_always_enters_the_body_on_its_initial_visit() -> None:
    output, audit = await _dispatcher().dispatch_inner(
        _node(),
        _run(visits=0),
        {"needs_repair": False},
    )

    assert output["zeroth_loop"]["quality-loop"] == {
        "route": "repeat",
        "attempt": 1,
        "retries_used": 0,
        "max_retries": 3,
        "termination_reason": None,
    }
    assert audit["execution_mode"] == "loop_control"
    assert audit["cost_usd"] == 0.0


@pytest.mark.asyncio
async def test_loop_node_routes_done_when_the_condition_matches_after_the_body() -> None:
    output, _ = await _dispatcher().dispatch_inner(
        _node(),
        _run(visits=1),
        {"needs_repair": False},
    )

    assert output["zeroth_loop"]["quality-loop"]["route"] == "done"
    assert output["zeroth_loop"]["quality-loop"]["termination_reason"] == "condition_met"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("completed_loop_visits", "expected_route", "expected_retries"),
    [
        (1, "repeat", 1),
        (2, "repeat", 2),
        (3, "repeat", 3),
        (4, "limit", 3),
    ],
)
async def test_max_retries_counts_additional_body_attempts(
    completed_loop_visits: int,
    expected_route: str,
    expected_retries: int,
) -> None:
    output, _ = await _dispatcher().dispatch_inner(
        _node(max_retries=3),
        _run(visits=completed_loop_visits),
        {"needs_repair": True},
    )

    state = output["zeroth_loop"]["quality-loop"]
    assert state["route"] == expected_route
    assert state["retries_used"] == expected_retries
    if expected_route == "limit":
        assert state["termination_reason"] == "max_retries_exhausted"
