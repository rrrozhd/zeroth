"""End-to-end integration tests for transform mappings.

Tests prove all four XFRM requirements are met:
- XFRM-01: Transform operation evaluates expression and writes to target
- XFRM-02: Same expression engine, access payload/state/variables
- XFRM-03: Output is plain Python value written to target_path
- XFRM-04: Side-effect-free, hardened -- only safe builtins allowed
"""

from __future__ import annotations

import pytest

from zeroth.core.mappings.errors import MappingExecutionError, MappingValidationError
from zeroth.core.mappings.executor import MappingExecutor
from zeroth.core.mappings.models import (
    ConstantMappingOperation,
    DefaultMappingOperation,
    EdgeMapping,
    PassthroughMappingOperation,
    RenameMappingOperation,
    TransformMappingOperation,
)
from zeroth.core.mappings.validator import MappingValidator


# ---------------------------------------------------------------------------
# XFRM-01: Transform operation evaluates expression and writes to target
# ---------------------------------------------------------------------------


class TestXfrm01TransformEvaluatesExpression:
    def test_transform_evaluates_arithmetic_expression(self) -> None:
        executor = MappingExecutor()
        mapping = EdgeMapping(
            operations=[
                TransformMappingOperation(
                    expression="payload.score * 100",
                    target_path="result.scaled",
                ),
            ]
        )
        output = executor.execute({"score": 85}, mapping)
        assert output == {"result": {"scaled": 8500}}

    def test_transform_evaluates_builtin_len(self) -> None:
        executor = MappingExecutor()
        mapping = EdgeMapping(
            operations=[
                TransformMappingOperation(
                    expression="len(payload.items)",
                    target_path="result.count",
                ),
            ]
        )
        output = executor.execute({"items": [1, 2, 3]}, mapping)
        assert output == {"result": {"count": 3}}

    def test_transform_evaluates_string_cast(self) -> None:
        executor = MappingExecutor()
        mapping = EdgeMapping(
            operations=[
                TransformMappingOperation(
                    expression="str(payload.count)",
                    target_path="result.text",
                ),
            ]
        )
        output = executor.execute({"count": 42}, mapping)
        assert output == {"result": {"text": "42"}}

    def test_transform_with_mixed_operations(self) -> None:
        executor = MappingExecutor()
        mapping = EdgeMapping(
            operations=[
                PassthroughMappingOperation(
                    source_path="name",
                    target_path="out.name",
                ),
                TransformMappingOperation(
                    expression="payload.score * 2",
                    target_path="out.doubled",
                ),
                ConstantMappingOperation(
                    target_path="out.source",
                    value="test",
                ),
            ]
        )
        output = executor.execute({"name": "Alice", "score": 50}, mapping)
        assert output == {"out": {"name": "Alice", "doubled": 100, "source": "test"}}


# ---------------------------------------------------------------------------
# XFRM-02: Same expression engine, access payload/state/variables
# ---------------------------------------------------------------------------


class TestXfrm02ContextNamespaceAccess:
    def test_transform_accesses_payload(self) -> None:
        executor = MappingExecutor()
        mapping = EdgeMapping(
            operations=[
                TransformMappingOperation(
                    expression="payload.value + 1",
                    target_path="result.x",
                ),
            ]
        )
        context = {"payload": {"value": 10}, "state": {}, "variables": {}}
        output = executor.execute({"value": 10}, mapping, context=context)
        assert output == {"result": {"x": 11}}

    def test_transform_accesses_state(self) -> None:
        executor = MappingExecutor()
        mapping = EdgeMapping(
            operations=[
                TransformMappingOperation(
                    expression="state.multiplier * payload.value",
                    target_path="result.x",
                ),
            ]
        )
        context = {
            "payload": {"value": 5},
            "state": {"multiplier": 3},
            "variables": {},
        }
        output = executor.execute({"value": 5}, mapping, context=context)
        assert output == {"result": {"x": 15}}

    def test_transform_accesses_variables(self) -> None:
        executor = MappingExecutor()
        mapping = EdgeMapping(
            operations=[
                TransformMappingOperation(
                    expression="variables.tax_rate * payload.price",
                    target_path="result.tax",
                ),
            ]
        )
        context = {
            "payload": {"price": 100},
            "state": {},
            "variables": {"tax_rate": 0.1},
        }
        output = executor.execute({"price": 100}, mapping, context=context)
        assert output == {"result": {"tax": 10.0}}

    def test_transform_uses_comparison_syntax(self) -> None:
        executor = MappingExecutor()
        mapping = EdgeMapping(
            operations=[
                TransformMappingOperation(
                    expression="payload.score > 50",
                    target_path="result.passed",
                ),
            ]
        )
        output = executor.execute({"score": 75}, mapping)
        assert output == {"result": {"passed": True}}

    def test_transform_uses_ternary_syntax(self) -> None:
        executor = MappingExecutor()
        mapping = EdgeMapping(
            operations=[
                TransformMappingOperation(
                    expression="'high' if payload.score > 50 else 'low'",
                    target_path="result.level",
                ),
            ]
        )
        output = executor.execute({"score": 75}, mapping)
        assert output == {"result": {"level": "high"}}


# ---------------------------------------------------------------------------
# XFRM-03: Output validated against target contract
# ---------------------------------------------------------------------------


class TestXfrm03OutputIsPlainValue:
    def test_transform_result_is_plain_value(self) -> None:
        executor = MappingExecutor()
        mapping = EdgeMapping(
            operations=[
                TransformMappingOperation(
                    expression="payload.x + payload.y",
                    target_path="result.sum",
                ),
            ]
        )
        output = executor.execute({"x": 3, "y": 4}, mapping)
        assert output == {"result": {"sum": 7}}
        assert isinstance(output["result"]["sum"], int)

    def test_transform_result_written_to_nested_path(self) -> None:
        executor = MappingExecutor()
        mapping = EdgeMapping(
            operations=[
                TransformMappingOperation(
                    expression="payload.value * 2",
                    target_path="result.computed.value",
                ),
            ]
        )
        output = executor.execute({"value": 21}, mapping)
        assert output == {"result": {"computed": {"value": 42}}}


# ---------------------------------------------------------------------------
# XFRM-04: Side-effect-free, hardened
# ---------------------------------------------------------------------------


class TestXfrm04SecurityHardening:
    def test_transform_rejects_import_at_validation(self) -> None:
        """__import__ uses ast.Attribute which triggers a different path,
        but the expression itself is rejected at runtime."""
        validator = MappingValidator()
        executor = MappingExecutor(validator=validator)
        mapping = EdgeMapping(
            operations=[
                TransformMappingOperation(
                    expression="__import__('os').system('echo nope')",
                    target_path="result.x",
                ),
            ]
        )
        # Static validation passes (ast.Call is allowed), but runtime rejects it
        with pytest.raises(MappingExecutionError, match="transform expression failed"):
            executor.execute({}, mapping)

    def test_transform_rejects_unsafe_call_at_runtime(self) -> None:
        executor = MappingExecutor()
        mapping = EdgeMapping(
            operations=[
                TransformMappingOperation(
                    expression="eval('1+1')",
                    target_path="result.x",
                ),
            ]
        )
        with pytest.raises(MappingExecutionError, match="transform expression failed"):
            executor.execute({}, mapping)

    def test_transform_rejects_open_at_runtime(self) -> None:
        executor = MappingExecutor()
        mapping = EdgeMapping(
            operations=[
                TransformMappingOperation(
                    expression="open('/etc/passwd')",
                    target_path="result.x",
                ),
            ]
        )
        with pytest.raises(MappingExecutionError, match="transform expression failed"):
            executor.execute({}, mapping)

    def test_transform_allows_only_safe_builtins(self) -> None:
        """Verify all 10 safe builtins work and unsafe ones are rejected."""
        executor = MappingExecutor()

        # All safe builtins should work
        safe_cases = [
            ("len([1,2,3])", 3),
            ("str(42)", "42"),
            ("int('7')", 7),
            ("float('3.14')", 3.14),
            ("bool(1)", True),
            ("abs(-5)", 5),
            ("min(1, 2)", 1),
            ("max(1, 2)", 2),
            ("round(3.14159, 2)", 3.14),
            ("sorted([3,1,2])", [1, 2, 3]),
        ]
        for expression, expected in safe_cases:
            mapping = EdgeMapping(
                operations=[
                    TransformMappingOperation(
                        expression=expression,
                        target_path="result.x",
                    ),
                ]
            )
            output = executor.execute({}, mapping)
            assert output["result"]["x"] == expected, f"Failed for {expression}"

        # Unsafe callables should be rejected
        unsafe_callables = ["exec('pass')", "compile('1', '', 'eval')", "getattr({}, '__class__')"]
        for expression in unsafe_callables:
            mapping = EdgeMapping(
                operations=[
                    TransformMappingOperation(
                        expression=expression,
                        target_path="result.x",
                    ),
                ]
            )
            with pytest.raises(MappingExecutionError, match="transform expression failed"):
                executor.execute({}, mapping)


# ---------------------------------------------------------------------------
# Backward compatibility: existing operations still work
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    def test_existing_passthrough_still_works(self) -> None:
        executor = MappingExecutor()
        mapping = EdgeMapping(
            operations=[
                PassthroughMappingOperation(
                    source_path="name",
                    target_path="out.name",
                ),
            ]
        )
        output = executor.execute({"name": "Alice"}, mapping)
        assert output == {"out": {"name": "Alice"}}

    def test_existing_rename_still_works(self) -> None:
        executor = MappingExecutor()
        mapping = EdgeMapping(
            operations=[
                RenameMappingOperation(
                    source_path="old_name",
                    target_path="new_name",
                ),
            ]
        )
        output = executor.execute({"old_name": "value"}, mapping)
        assert output == {"new_name": "value"}

    def test_existing_constant_still_works(self) -> None:
        executor = MappingExecutor()
        mapping = EdgeMapping(
            operations=[
                ConstantMappingOperation(
                    target_path="out.version",
                    value="1.0",
                ),
            ]
        )
        output = executor.execute({}, mapping)
        assert output == {"out": {"version": "1.0"}}

    def test_existing_default_still_works(self) -> None:
        executor = MappingExecutor()
        # With source present
        mapping = EdgeMapping(
            operations=[
                DefaultMappingOperation(
                    source_path="score",
                    target_path="out.score",
                    default_value=0,
                ),
            ]
        )
        output = executor.execute({"score": 95}, mapping)
        assert output == {"out": {"score": 95}}

        # With source missing -- should use default
        output = executor.execute({}, mapping)
        assert output == {"out": {"score": 0}}

        # Without source_path -- always use default
        mapping2 = EdgeMapping(
            operations=[
                DefaultMappingOperation(
                    target_path="out.flag",
                    default_value=True,
                ),
            ]
        )
        output = executor.execute({}, mapping2)
        assert output == {"out": {"flag": True}}

    def test_all_legacy_operations_combined(self) -> None:
        executor = MappingExecutor()
        mapping = EdgeMapping(
            operations=[
                PassthroughMappingOperation(
                    source_path="name",
                    target_path="out.name",
                ),
                RenameMappingOperation(
                    source_path="user_id",
                    target_path="out.id",
                ),
                ConstantMappingOperation(
                    target_path="out.source",
                    value="system",
                ),
                DefaultMappingOperation(
                    source_path="score",
                    target_path="out.score",
                    default_value=0,
                ),
            ]
        )
        output = executor.execute({"name": "Alice", "user_id": "u-1", "score": 85}, mapping)
        assert output == {
            "out": {
                "name": "Alice",
                "id": "u-1",
                "source": "system",
                "score": 85,
            }
        }
