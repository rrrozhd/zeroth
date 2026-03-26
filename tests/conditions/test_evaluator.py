from __future__ import annotations

import pytest

from zeroth.conditions import ConditionContext, ConditionEvaluator
from zeroth.conditions.errors import ConditionEvaluationError
from zeroth.graph.models import Condition as GraphCondition


def test_condition_evaluator_handles_nested_paths_and_metadata() -> None:
    evaluator = ConditionEvaluator()
    context = ConditionContext(
        payload={"user": {"id": "u-1", "score": 17}},
        state={"loop": 1},
        variables={"threshold": 10},
    )
    condition = GraphCondition(
        expression="payload.user.id is not None and payload.user.score > variables.threshold",
    )

    result = evaluator.evaluate(
        condition,
        context,
        edge_id="edge-ab",
        source_node_id="node-a",
        target_node_id="node-b",
        metadata={"origin": "test"},
    )

    assert result.matched is True
    assert result.selected_edge_id == "edge-ab"
    assert result.details["value"] is True
    assert result.details["source_node_id"] == "node-a"
    assert result.details["target_node_id"] == "node-b"
    assert result.details["graph_context"] == {"origin": "test"}


def test_condition_evaluator_supports_operand_refs() -> None:
    evaluator = ConditionEvaluator()
    context = ConditionContext(payload={"flags": {"left": True, "right": False}})
    condition = GraphCondition(
        expression="flags.left",
        branch_rule="all",
        operand_refs=["payload.flags.left", "payload.flags.right"],
    )

    result = evaluator.evaluate(condition, context, edge_id="edge-ab")

    assert result.matched is False
    assert result.details["value"] == [True, False]


def test_condition_evaluator_rejects_unsupported_calls() -> None:
    evaluator = ConditionEvaluator()
    context = ConditionContext(payload={"user": {"id": "u-1"}})
    condition = GraphCondition(expression="__import__('os').system('echo nope')")

    with pytest.raises(ConditionEvaluationError, match="unsupported expression node"):
        evaluator.evaluate(condition, context)
