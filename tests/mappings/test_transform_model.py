"""Tests for TransformMappingOperation model and MappingExecutionError."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from zeroth.core.mappings.errors import MappingExecutionError
from zeroth.core.mappings.models import (
    EdgeMapping,
    MappingOperation,
    TransformMappingOperation,
)


class TestTransformMappingOperation:
    """TransformMappingOperation model tests."""

    def test_instantiate_with_required_fields(self) -> None:
        op = TransformMappingOperation(
            expression="payload.score * 100",
            target_path="result.score",
        )
        assert op.operation == "transform"
        assert op.expression == "payload.score * 100"
        assert op.target_path == "result.score"

    def test_operation_literal_default(self) -> None:
        op = TransformMappingOperation(
            expression="payload.x + payload.y",
            target_path="result.sum",
        )
        assert op.operation == "transform"

    def test_expression_is_required(self) -> None:
        with pytest.raises(ValidationError):
            TransformMappingOperation(target_path="result.score")  # type: ignore[call-arg]

    def test_target_path_is_required(self) -> None:
        with pytest.raises(ValidationError):
            TransformMappingOperation(expression="payload.x")  # type: ignore[call-arg]

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            TransformMappingOperation(
                expression="payload.x",
                target_path="result.x",
                extra_field="not_allowed",
            )

    def test_inherits_from_mapping_operation_base(self) -> None:
        op = TransformMappingOperation(
            expression="payload.x",
            target_path="result.x",
        )
        assert hasattr(op, "target_path")


class TestMappingOperationUnion:
    """TransformMappingOperation participates in the MappingOperation union."""

    def test_union_accepts_transform_via_discriminator(self) -> None:
        """MappingOperation union should accept a transform operation."""
        edge = EdgeMapping(
            operations=[
                {"operation": "transform", "expression": "payload.x * 2", "target_path": "result.x"}
            ]
        )
        assert len(edge.operations) == 1
        op = edge.operations[0]
        assert isinstance(op, TransformMappingOperation)
        assert op.expression == "payload.x * 2"

    def test_edge_mapping_json_round_trip(self) -> None:
        """Transform operations should round-trip through JSON serialization."""
        edge = EdgeMapping(
            operations=[
                TransformMappingOperation(
                    expression="payload.score * 100",
                    target_path="result.computed_score",
                )
            ]
        )
        json_str = edge.model_dump_json()
        restored = EdgeMapping.model_validate_json(json_str)
        assert len(restored.operations) == 1
        op = restored.operations[0]
        assert isinstance(op, TransformMappingOperation)
        assert op.expression == "payload.score * 100"
        assert op.target_path == "result.computed_score"


class TestMappingExecutionError:
    """MappingExecutionError tests."""

    def test_is_value_error_subclass(self) -> None:
        assert issubclass(MappingExecutionError, ValueError)

    def test_can_be_raised_and_caught(self) -> None:
        with pytest.raises(MappingExecutionError, match="test error"):
            raise MappingExecutionError("test error")

    def test_caught_by_value_error(self) -> None:
        with pytest.raises(ValueError):
            raise MappingExecutionError("should be caught by ValueError")


class TestPublicApiExports:
    """TransformMappingOperation and MappingExecutionError are exported from the package."""

    def test_transform_mapping_operation_importable(self) -> None:
        from zeroth.core.mappings import TransformMappingOperation as Imported

        assert Imported is TransformMappingOperation

    def test_mapping_execution_error_importable(self) -> None:
        from zeroth.core.mappings import MappingExecutionError as Imported

        assert Imported is MappingExecutionError
