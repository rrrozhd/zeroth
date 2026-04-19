from __future__ import annotations

import pytest

from zeroth.core.mappings.errors import MappingValidationError
from zeroth.core.mappings.models import (
    ConstantMappingOperation,
    EdgeMapping,
    PassthroughMappingOperation,
    RenameMappingOperation,
    TransformMappingOperation,
)
from zeroth.core.mappings.validator import MappingValidator


def test_mapping_validator_accepts_valid_mapping() -> None:
    validator = MappingValidator()
    mapping = EdgeMapping(
        operations=[
            PassthroughMappingOperation(
                source_path="payload.user.name",
                target_path="request.user.name",
            ),
            ConstantMappingOperation(target_path="request.source", value="zeroth"),
        ]
    )

    validator.validate(mapping)


def test_mapping_validator_rejects_duplicate_targets() -> None:
    validator = MappingValidator()
    mapping = EdgeMapping(
        operations=[
            PassthroughMappingOperation(
                source_path="payload.user.name",
                target_path="request.user.name",
            ),
            RenameMappingOperation(
                source_path="payload.user.id",
                target_path="request.user.name",
            ),
        ]
    )

    with pytest.raises(MappingValidationError, match="duplicate target path"):
        validator.validate(mapping)


def test_mapping_validator_rejects_empty_paths() -> None:
    validator = MappingValidator()
    mapping = EdgeMapping(
        operations=[
            ConstantMappingOperation(target_path="", value="zeroth"),
        ]
    )

    with pytest.raises(MappingValidationError, match="target_path must not be empty"):
        validator.validate(mapping)


# -- Transform validation tests --


class TestTransformValidation:
    """Tests for transform operation validation with static expression checking."""

    def test_accepts_valid_transform(self) -> None:
        validator = MappingValidator()
        mapping = EdgeMapping(
            operations=[
                TransformMappingOperation(
                    expression="payload.score * 100",
                    target_path="result.score",
                ),
            ]
        )
        validator.validate(mapping)

    def test_accepts_comparison_expression(self) -> None:
        validator = MappingValidator()
        mapping = EdgeMapping(
            operations=[
                TransformMappingOperation(
                    expression="payload.score > 50",
                    target_path="result.passed",
                ),
            ]
        )
        validator.validate(mapping)

    def test_accepts_addition_expression(self) -> None:
        validator = MappingValidator()
        mapping = EdgeMapping(
            operations=[
                TransformMappingOperation(
                    expression="payload.x + payload.y",
                    target_path="result.sum",
                ),
            ]
        )
        validator.validate(mapping)

    def test_accepts_equality_expression(self) -> None:
        validator = MappingValidator()
        mapping = EdgeMapping(
            operations=[
                TransformMappingOperation(
                    expression="payload.status == 'done'",
                    target_path="result.is_done",
                ),
            ]
        )
        validator.validate(mapping)

    def test_accepts_ternary_expression(self) -> None:
        validator = MappingValidator()
        mapping = EdgeMapping(
            operations=[
                TransformMappingOperation(
                    expression="payload.a if payload.b else payload.c",
                    target_path="result.choice",
                ),
            ]
        )
        validator.validate(mapping)

    def test_rejects_empty_expression(self) -> None:
        validator = MappingValidator()
        mapping = EdgeMapping(
            operations=[
                TransformMappingOperation(
                    expression="",
                    target_path="result.x",
                ),
            ]
        )
        with pytest.raises(MappingValidationError, match="must not be empty"):
            validator.validate(mapping)

    def test_rejects_whitespace_only_expression(self) -> None:
        validator = MappingValidator()
        mapping = EdgeMapping(
            operations=[
                TransformMappingOperation(
                    expression="   ",
                    target_path="result.x",
                ),
            ]
        )
        with pytest.raises(MappingValidationError, match="must not be empty"):
            validator.validate(mapping)

    def test_rejects_invalid_syntax(self) -> None:
        validator = MappingValidator()
        mapping = EdgeMapping(
            operations=[
                TransformMappingOperation(
                    expression="payload.score ***",
                    target_path="result.x",
                ),
            ]
        )
        with pytest.raises(MappingValidationError, match="invalid transform expression syntax"):
            validator.validate(mapping)

    def test_allows_call_nodes_in_static_validation(self) -> None:
        """ast.Call is now allowed by the validator since safe builtins are enforced at runtime."""
        validator = MappingValidator()
        mapping = EdgeMapping(
            operations=[
                TransformMappingOperation(
                    expression="len(payload.items)",
                    target_path="result.x",
                ),
            ]
        )
        # Should not raise -- ast.Call is permitted; runtime evaluator enforces the allowlist
        validator.validate(mapping)

    def test_rejects_unsupported_ast_node_lambda(self) -> None:
        validator = MappingValidator()
        mapping = EdgeMapping(
            operations=[
                TransformMappingOperation(
                    expression="(lambda: 1)()",
                    target_path="result.x",
                ),
            ]
        )
        with pytest.raises(MappingValidationError, match="unsupported expression node"):
            validator.validate(mapping)

    def test_rejects_duplicate_target_path(self) -> None:
        validator = MappingValidator()
        mapping = EdgeMapping(
            operations=[
                TransformMappingOperation(
                    expression="payload.x * 2",
                    target_path="result.score",
                ),
                TransformMappingOperation(
                    expression="payload.y * 3",
                    target_path="result.score",
                ),
            ]
        )
        with pytest.raises(MappingValidationError, match="duplicate target path"):
            validator.validate(mapping)

    def test_rejects_empty_target_path(self) -> None:
        validator = MappingValidator()
        mapping = EdgeMapping(
            operations=[
                TransformMappingOperation(
                    expression="payload.x",
                    target_path="",
                ),
            ]
        )
        with pytest.raises(MappingValidationError, match="target_path must not be empty"):
            validator.validate(mapping)

    def test_rejects_malformed_target_path(self) -> None:
        validator = MappingValidator()
        mapping = EdgeMapping(
            operations=[
                TransformMappingOperation(
                    expression="payload.x",
                    target_path="result..score",
                ),
            ]
        )
        with pytest.raises(MappingValidationError, match="dot-separated path segments"):
            validator.validate(mapping)
