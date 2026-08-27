from __future__ import annotations

import pytest

from zeroth.contracts.conditions.errors import ConditionEvaluationError
from zeroth.contracts.graph import IfNode, IfNodeData
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


def _node(expression: str = "payload.score >= 0.8") -> IfNode:
    return IfNode(
        node_id="quality-gate",
        graph_version_ref="quality:v1",
        condition=IfNodeData(expression=expression),
    )


def _run() -> Run:
    return Run(graph_version_ref="quality:v1", deployment_ref="quality-v1")


@pytest.mark.asyncio
async def test_if_node_allows_an_incomplete_draft_but_refuses_to_execute_it() -> None:
    node = _node("")

    with pytest.raises(ConditionEvaluationError):
        await _dispatcher().dispatch_inner(node, _run(), {})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("score", "expected_route"),
    [(0.91, "true"), (0.79, "false")],
)
async def test_if_node_routes_without_invoking_an_external_runner(
    score: float,
    expected_route: str,
) -> None:
    output, audit = await _dispatcher().dispatch_inner(
        _node(),
        _run(),
        {"score": score, "request_id": "req-1"},
    )

    assert output == {
        "score": score,
        "request_id": "req-1",
        "zeroth_if": {
            "quality-gate": {
                "route": expected_route,
                "matched": expected_route == "true",
            }
        },
    }
    assert audit == {
        "execution_mode": "if_control",
        "condition_id": "quality-gate:expression",
        "expression_sha256": "8dae321ea28193fdd9b559054d4ca2123091f0569c5700cbc9af2789de2b51c3",
        "route": expected_route,
        "matched": expected_route == "true",
        "cost_usd": 0.0,
        "estimated_cost_usd": 0.0,
        "cost_measurement": "measured",
    }
