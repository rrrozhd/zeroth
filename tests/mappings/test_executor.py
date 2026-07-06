from __future__ import annotations

import pytest

from zeroth.core.mappings.errors import MappingExecutionError
from zeroth.core.mappings.executor import MappingExecutor
from zeroth.core.mappings.models import (
    ConstantMappingOperation,
    DefaultMappingOperation,
    EdgeMapping,
    PassthroughMappingOperation,
    RenameMappingOperation,
    TransformMappingOperation,
)


# -- Existing tests (unchanged) --


def test_mapping_executor_applies_nested_operations() -> None:
    executor = MappingExecutor()
    mapping = EdgeMapping(
        operations=[
            PassthroughMappingOperation(
                source_path="payload.user.name",
                target_path="request.user.name",
            ),
            RenameMappingOperation(
                source_path="payload.user.id",
                target_path="request.user.identifier",
            ),
            ConstantMappingOperation(
                target_path="request.source",
                value="zeroth",
            ),
            DefaultMappingOperation(
                source_path="payload.user.locale",
                target_path="request.user.locale",
                default_value="en-US",
            ),
        ]
    )

    output = executor.execute(
        {"payload": {"user": {"name": "Ada", "id": 7}}},
        mapping,
    )

    assert output == {
        "request": {
            "source": "zeroth",
            "user": {
                "identifier": 7,
                "locale": "en-US",
                "name": "Ada",
            },
        }
    }


def test_mapping_executor_uses_source_value_before_default() -> None:
    executor = MappingExecutor()
    mapping = EdgeMapping(
        operations=[
            DefaultMappingOperation(
                source_path="payload.user.locale",
                target_path="request.user.locale",
                default_value="en-US",
            )
        ]
    )

    output = executor.execute(
        {"payload": {"user": {"locale": "pt-BR"}}},
        mapping,
    )

    assert output == {"request": {"user": {"locale": "pt-BR"}}}


# -- Transform execution tests --


class TestTransformExecution:
    """Tests for transform operation execution via _SafeEvaluator."""

    def test_basic_transform_multiply(self) -> None:
        """Transform evaluates expression against payload and writes to target_path."""
        executor = MappingExecutor()
        mapping = EdgeMapping(
            operations=[
                TransformMappingOperation(
                    expression="payload.score * 100",
                    target_path="result.computed_score",
                ),
            ]
        )
        output = executor.execute(
            {"score": 85},
            mapping,
            context={"payload": {"score": 85}},
        )
        assert output == {"result": {"computed_score": 8500}}

    def test_transform_with_state_context(self) -> None:
        """Transform can access state from context."""
        executor = MappingExecutor()
        mapping = EdgeMapping(
            operations=[
                TransformMappingOperation(
                    expression="state.multiplier + payload.value",
                    target_path="result.total",
                ),
            ]
        )
        output = executor.execute(
            {"value": 5},
            mapping,
            context={
                "payload": {"value": 5},
                "state": {"multiplier": 10},
            },
        )
        assert output == {"result": {"total": 15}}

    def test_transform_with_variables_context(self) -> None:
        """Transform can access variables from context."""
        executor = MappingExecutor()
        mapping = EdgeMapping(
            operations=[
                TransformMappingOperation(
                    expression="variables.tax_rate * payload.price",
                    target_path="result.tax",
                ),
            ]
        )
        output = executor.execute(
            {"price": 100},
            mapping,
            context={
                "payload": {"price": 100},
                "variables": {"tax_rate": 0.1},
            },
        )
        assert output == {"result": {"tax": 10.0}}

    def test_backward_compat_no_context(self) -> None:
        """Existing operations work without context parameter."""
        executor = MappingExecutor()
        mapping = EdgeMapping(
            operations=[
                PassthroughMappingOperation(
                    source_path="payload.name",
                    target_path="result.name",
                ),
            ]
        )
        output = executor.execute(
            {"payload": {"name": "Ada"}},
            mapping,
        )
        assert output == {"result": {"name": "Ada"}}

    def test_transform_without_context_uses_payload_only(self) -> None:
        """Transform without context builds minimal namespace from payload."""
        executor = MappingExecutor()
        mapping = EdgeMapping(
            operations=[
                TransformMappingOperation(
                    expression="payload.x + payload.y",
                    target_path="result.sum",
                ),
            ]
        )
        output = executor.execute(
            {"x": 3, "y": 7},
            mapping,
        )
        assert output == {"result": {"sum": 10}}

    def test_transform_division_by_zero_raises_mapping_execution_error(self) -> None:
        """Division by zero in expression raises MappingExecutionError, not ConditionEvaluationError."""
        executor = MappingExecutor()
        mapping = EdgeMapping(
            operations=[
                TransformMappingOperation(
                    expression="payload.x / 0",
                    target_path="result.bad",
                ),
            ]
        )
        with pytest.raises(MappingExecutionError, match="transform expression failed"):
            executor.execute(
                {"x": 10},
                mapping,
                context={"payload": {"x": 10}},
            )

    def test_transform_missing_attribute_returns_none(self) -> None:
        """Missing attribute in expression returns None per evaluator design."""
        executor = MappingExecutor()
        mapping = EdgeMapping(
            operations=[
                TransformMappingOperation(
                    expression="payload.nonexistent",
                    target_path="result.val",
                ),
            ]
        )
        output = executor.execute(
            {},
            mapping,
            context={"payload": {}},
        )
        assert output == {"result": {"val": None}}

    def test_mixed_operations_passthrough_transform_constant(self) -> None:
        """Passthrough + transform + constant in same EdgeMapping produces correct combined output."""
        executor = MappingExecutor()
        mapping = EdgeMapping(
            operations=[
                PassthroughMappingOperation(
                    source_path="payload.name",
                    target_path="result.name",
                ),
                TransformMappingOperation(
                    expression="payload.score * 2",
                    target_path="result.doubled_score",
                ),
                ConstantMappingOperation(
                    target_path="result.source",
                    value="zeroth",
                ),
            ]
        )
        output = executor.execute(
            {"payload": {"name": "Ada", "score": 50}},
            mapping,
            context={"payload": {"name": "Ada", "score": 50}},
        )
        assert output == {
            "result": {
                "name": "Ada",
                "doubled_score": 100,
                "source": "zeroth",
            }
        }

    def test_ternary_expression(self) -> None:
        """Ternary expression works in transform."""
        executor = MappingExecutor()
        mapping = EdgeMapping(
            operations=[
                TransformMappingOperation(
                    expression="payload.score if payload.score > 0 else 0",
                    target_path="result.safe_score",
                ),
            ]
        )
        output = executor.execute(
            {"score": 42},
            mapping,
            context={"payload": {"score": 42}},
        )
        assert output == {"result": {"safe_score": 42}}

    def test_ternary_expression_false_branch(self) -> None:
        """Ternary expression takes else branch when test is falsy."""
        executor = MappingExecutor()
        mapping = EdgeMapping(
            operations=[
                TransformMappingOperation(
                    expression="payload.score if payload.score > 0 else 0",
                    target_path="result.safe_score",
                ),
            ]
        )
        output = executor.execute(
            {"score": -5},
            mapping,
            context={"payload": {"score": -5}},
        )
        assert output == {"result": {"safe_score": 0}}

    def test_boolean_expression_and(self) -> None:
        """Boolean 'and' expression returns True/False."""
        executor = MappingExecutor()
        mapping = EdgeMapping(
            operations=[
                TransformMappingOperation(
                    expression="payload.active and payload.verified",
                    target_path="result.eligible",
                ),
            ]
        )
        output = executor.execute(
            {"active": True, "verified": True},
            mapping,
            context={"payload": {"active": True, "verified": True}},
        )
        assert output == {"result": {"eligible": True}}

    def test_boolean_expression_and_false(self) -> None:
        """Boolean 'and' returns False when one operand is falsy."""
        executor = MappingExecutor()
        mapping = EdgeMapping(
            operations=[
                TransformMappingOperation(
                    expression="payload.active and payload.verified",
                    target_path="result.eligible",
                ),
            ]
        )
        output = executor.execute(
            {"active": True, "verified": False},
            mapping,
            context={"payload": {"active": True, "verified": False}},
        )
        assert output == {"result": {"eligible": False}}
