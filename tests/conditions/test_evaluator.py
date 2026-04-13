from __future__ import annotations

import pytest

from zeroth.core.conditions import ConditionContext, ConditionEvaluator
from zeroth.core.conditions.errors import ConditionEvaluationError
from zeroth.core.graph.models import Condition as GraphCondition


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

    with pytest.raises(ConditionEvaluationError, match="not an allowed safe builtin"):
        evaluator.evaluate(condition, context)


# ---------------------------------------------------------------------------
# Safe builtins support in _SafeEvaluator
# ---------------------------------------------------------------------------

from zeroth.core.conditions.evaluator import _SafeEvaluator


class TestSafeEvaluatorBuiltinLen:
    def test_len_of_literal_list(self) -> None:
        ev = _SafeEvaluator({"payload": {"items": [1, 2, 3]}})
        assert ev.evaluate("len([1, 2, 3])") == 3

    def test_len_of_namespace_list(self) -> None:
        ev = _SafeEvaluator({"payload": {"items": [1, 2, 3]}})
        assert ev.evaluate("len(payload.items)") == 3


class TestSafeEvaluatorBuiltinStr:
    def test_str_of_int(self) -> None:
        ev = _SafeEvaluator({"payload": {"count": 42}})
        assert ev.evaluate("str(payload.count)") == "42"


class TestSafeEvaluatorBuiltinInt:
    def test_int_of_string(self) -> None:
        ev = _SafeEvaluator({"payload": {"text": "7"}})
        assert ev.evaluate("int(payload.text)") == 7


class TestSafeEvaluatorBuiltinFloat:
    def test_float_of_string(self) -> None:
        ev = _SafeEvaluator({"payload": {"text": "3.14"}})
        assert ev.evaluate("float(payload.text)") == 3.14


class TestSafeEvaluatorBuiltinBool:
    def test_bool_of_zero(self) -> None:
        ev = _SafeEvaluator({"payload": {"value": 0}})
        assert ev.evaluate("bool(payload.value)") is False


class TestSafeEvaluatorBuiltinAbs:
    def test_abs_of_negative(self) -> None:
        ev = _SafeEvaluator({"payload": {"delta": -5}})
        assert ev.evaluate("abs(payload.delta)") == 5


class TestSafeEvaluatorBuiltinMinMax:
    def test_min_of_two(self) -> None:
        ev = _SafeEvaluator({"payload": {"a": 10, "b": 3}})
        assert ev.evaluate("min(payload.a, payload.b)") == 3

    def test_max_of_two(self) -> None:
        ev = _SafeEvaluator({"payload": {"a": 10, "b": 3}})
        assert ev.evaluate("max(payload.a, payload.b)") == 10


class TestSafeEvaluatorBuiltinRound:
    def test_round_with_precision(self) -> None:
        ev = _SafeEvaluator({"payload": {"value": 3.14159}})
        assert ev.evaluate("round(payload.value, 2)") == 3.14


class TestSafeEvaluatorBuiltinSorted:
    def test_sorted_list(self) -> None:
        ev = _SafeEvaluator({"payload": {"items": [3, 1, 2]}})
        assert ev.evaluate("sorted(payload.items)") == [1, 2, 3]


class TestSafeEvaluatorRejectsUnsafeCallables:
    def test_rejects_eval(self) -> None:
        ev = _SafeEvaluator({})
        with pytest.raises(ConditionEvaluationError, match="not an allowed safe builtin"):
            ev.evaluate("eval('1+1')")

    def test_rejects_print(self) -> None:
        ev = _SafeEvaluator({})
        with pytest.raises(ConditionEvaluationError, match="not an allowed safe builtin"):
            ev.evaluate("print('hello')")

    def test_rejects_open(self) -> None:
        ev = _SafeEvaluator({})
        with pytest.raises(ConditionEvaluationError, match="not an allowed safe builtin"):
            ev.evaluate("open('/etc/passwd')")

    def test_import_still_rejected(self) -> None:
        """__import__ is rejected at the ast.Call level before reaching Name resolution."""
        ev = _SafeEvaluator({})
        with pytest.raises(ConditionEvaluationError):
            ev.evaluate("__import__('os').system('echo nope')")
