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


def _multi_route_node() -> IfNode:
    return IfNode(
        node_id="priority-gate",
        graph_version_ref="priority:v1",
        condition=IfNodeData(
            expression="payload.priority",
            routes=[
                {"route_id": "critical", "label": "Critical", "match_value": "p0"},
                {"route_id": "normal", "label": "Normal", "match_value": "p1"},
                {"route_id": "fallback", "label": "Other", "is_default": True},
            ],
        ),
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("priority", "expected_route"),
    [("p0", "critical"), ("p1", "normal"), ("p2", "fallback")],
)
async def test_if_node_routes_scalar_results_across_named_cases(
    priority: str,
    expected_route: str,
) -> None:
    output, audit = await _dispatcher().dispatch_inner(
        _multi_route_node(),
        _run(),
        {"priority": priority},
    )

    assert output["zeroth_if"]["priority-gate"] == {
        "route": expected_route,
        "value": priority,
    }
    assert audit["route"] == expected_route
    assert audit["value"] == priority
