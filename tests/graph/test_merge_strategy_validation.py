"""Publish-time validation tests for parallel merge strategies (Phase 43-02).

Covers ``GraphValidator._validate_parallel_configs`` and
``_check_merge_dict_contract`` including:
    * Backward compat (no parallel_config on any node).
    * Valid custom reducer_ref resolution.
    * Invalid reducer_ref regex / importlib / not-callable.
    * Merge strategy output contract dict-shape check.
    * Graceful degradation when no ContractRegistry is wired.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from zeroth.core.contracts.registry import ContractVersion
from zeroth.core.graph.models import Graph
from zeroth.core.graph.validation import GraphValidator
from zeroth.core.graph.validation_errors import (
    GraphValidationError,
    ValidationCode,
    ValidationSeverity,
)
from zeroth.core.parallel.errors import ReducerRefValidationError
from zeroth.core.parallel.models import ParallelConfig

from tests.graph.test_validation import build_valid_graph


# =========================================================================
# In-memory stub ContractRegistry
# =========================================================================


class StubContractRegistry:
    """Minimal in-memory ContractRegistry stand-in for validation tests.

    Only implements ``get(name, version=None)`` returning a pre-built
    ``ContractVersion`` with a configurable ``json_schema``. Raises
    ``KeyError`` for unknown names to exercise the registry-failure path.
    """

    def __init__(self, contracts: dict[str, dict[str, Any]]) -> None:
        self._contracts = contracts

    async def get(
        self, name: str, version: int | None = None
    ) -> ContractVersion:
        if name not in self._contracts:
            raise KeyError(f"contract {name!r} not in stub")
        return ContractVersion(
            name=name,
            version=1,
            model_path="tests._fixtures.reducers:sum_scores",
            json_schema=self._contracts[name],
            metadata={},
            created_at="2026-04-15T00:00:00Z",
        )


def _attach_parallel_config(graph: Graph, config: ParallelConfig) -> Graph:
    """Return a copy of ``graph`` with ``config`` attached to the first node."""
    new_nodes = list(graph.nodes)
    new_nodes[0] = new_nodes[0].model_copy(update={"parallel_config": config})
    return graph.model_copy(update={"nodes": new_nodes})


# =========================================================================
# Backward compatibility
# =========================================================================


class TestValidateParallelConfigsBackwardCompat:
    async def test_no_parallel_config_anywhere(self) -> None:
        validator = GraphValidator()
        report = await validator.validate(build_valid_graph())
        assert report.is_valid

    async def test_valid_collect_strategy(self) -> None:
        validator = GraphValidator()
        graph = _attach_parallel_config(
            build_valid_graph(),
            ParallelConfig(split_path="items", merge_strategy="collect"),
        )
        report = await validator.validate(graph)
        assert report.is_valid

    async def test_valid_reduce_strategy_no_reducer_ref(self) -> None:
        validator = GraphValidator()
        graph = _attach_parallel_config(
            build_valid_graph(),
            ParallelConfig(split_path="items", merge_strategy="reduce"),
        )
        report = await validator.validate(graph)
        # reduce does NOT trigger reducer_ref or dict-contract checks.
        assert report.is_valid


# =========================================================================
# reducer_ref validation
# =========================================================================


class TestReducerRefValidation:
    async def test_custom_with_valid_reducer_ref_passes(self) -> None:
        validator = GraphValidator()
        graph = _attach_parallel_config(
            build_valid_graph(),
            ParallelConfig(
                split_path="items",
                merge_strategy="custom",
                reducer_ref="tests._fixtures.reducers.sum_scores",
            ),
        )
        report = await validator.validate(graph)
        assert report.is_valid, f"unexpected issues: {report.issues}"

    async def test_custom_with_nonexistent_module_fails(self) -> None:
        validator = GraphValidator()
        graph = _attach_parallel_config(
            build_valid_graph(),
            ParallelConfig(
                split_path="items",
                merge_strategy="custom",
                reducer_ref="nonexistent_xyz.mod.fn",
            ),
        )
        report = await validator.validate(graph)
        codes = {i.code for i in report.issues}
        assert ValidationCode.INVALID_REDUCER_REF in codes

    async def test_custom_with_not_callable_fails(self) -> None:
        validator = GraphValidator()
        graph = _attach_parallel_config(
            build_valid_graph(),
            ParallelConfig(
                split_path="items",
                merge_strategy="custom",
                reducer_ref="tests._fixtures.reducers.NOT_CALLABLE",
            ),
        )
        report = await validator.validate(graph)
        codes = {i.code for i in report.issues}
        assert ValidationCode.INVALID_REDUCER_REF in codes

    async def test_regex_rejects_before_importlib(self) -> None:
        # Ensure a bad-looking reducer_ref never reaches importlib. We cannot
        # construct the ParallelConfig with a bad ref AND merge_strategy=custom
        # via the normal constructor without the regex check triggering at
        # resolve time — but we can patch importlib and confirm it's not called.
        validator = GraphValidator()
        graph = _attach_parallel_config(
            build_valid_graph(),
            ParallelConfig(
                split_path="items",
                merge_strategy="custom",
                reducer_ref="os",  # single segment, regex-rejected
            ),
        )
        with patch("zeroth.core.parallel.reducers.importlib.import_module") as mock_imp:
            report = await validator.validate(graph)
            mock_imp.assert_not_called()
        codes = {i.code for i in report.issues}
        assert ValidationCode.INVALID_REDUCER_REF in codes

    async def test_validate_or_raise_raises_on_bad_ref(self) -> None:
        validator = GraphValidator()
        graph = _attach_parallel_config(
            build_valid_graph(),
            ParallelConfig(
                split_path="items",
                merge_strategy="custom",
                reducer_ref="nonexistent_xyz.mod.fn",
            ),
        )
        with pytest.raises(GraphValidationError):
            await validator.validate_or_raise(graph)


# =========================================================================
# merge strategy dict-contract check
# =========================================================================


class TestMergeDictContractCheck:
    async def test_merge_with_object_contract_passes(self) -> None:
        registry = StubContractRegistry(
            {"contract://agent.output": {"type": "object", "properties": {}}}
        )
        validator = GraphValidator(contract_registry=registry)  # type: ignore[arg-type]
        graph = _attach_parallel_config(
            build_valid_graph(),
            ParallelConfig(split_path="items", merge_strategy="merge"),
        )
        report = await validator.validate(graph)
        assert report.is_valid, f"unexpected issues: {report.issues}"

    async def test_merge_with_array_contract_fails(self) -> None:
        registry = StubContractRegistry(
            {"contract://agent.output": {"type": "array", "items": {}}}
        )
        validator = GraphValidator(contract_registry=registry)  # type: ignore[arg-type]
        graph = _attach_parallel_config(
            build_valid_graph(),
            ParallelConfig(split_path="items", merge_strategy="merge"),
        )
        report = await validator.validate(graph)
        assert not report.is_valid
        codes = {i.code for i in report.issues}
        assert ValidationCode.INVALID_MERGE_STRATEGY in codes
        msgs = " ".join(i.message for i in report.issues)
        assert "type='array'" in msgs or "type=" in msgs

    async def test_merge_with_unknown_contract_fails(self) -> None:
        registry = StubContractRegistry({})  # empty — any lookup raises
        validator = GraphValidator(contract_registry=registry)  # type: ignore[arg-type]
        graph = _attach_parallel_config(
            build_valid_graph(),
            ParallelConfig(split_path="items", merge_strategy="merge"),
        )
        report = await validator.validate(graph)
        codes = {i.code for i in report.issues}
        assert ValidationCode.INVALID_MERGE_STRATEGY in codes

    async def test_merge_with_missing_output_contract_ref_fails(self) -> None:
        registry = StubContractRegistry({})
        validator = GraphValidator(contract_registry=registry)  # type: ignore[arg-type]
        base = build_valid_graph()
        # Strip the output_contract_ref on node 0 before attaching parallel_config.
        nodes = list(base.nodes)
        nodes[0] = nodes[0].model_copy(
            update={
                "output_contract_ref": None,
                "parallel_config": ParallelConfig(
                    split_path="items", merge_strategy="merge"
                ),
            }
        )
        graph = base.model_copy(update={"nodes": nodes})
        report = await validator.validate(graph)
        codes = {i.code for i in report.issues}
        assert ValidationCode.INVALID_MERGE_STRATEGY in codes


# =========================================================================
# Degraded mode (no ContractRegistry wired)
# =========================================================================


class TestValidatorDegradation:
    async def test_merge_without_registry_degrades_to_warning(self) -> None:
        validator = GraphValidator()  # no contract_registry
        graph = _attach_parallel_config(
            build_valid_graph(),
            ParallelConfig(split_path="items", merge_strategy="merge"),
        )
        report = await validator.validate(graph)
        # Warning emitted but report is still valid (no blocking errors).
        assert report.is_valid
        warnings = [
            i for i in report.issues if i.severity == ValidationSeverity.WARNING
        ]
        assert any(
            i.code == ValidationCode.INVALID_MERGE_STRATEGY for i in warnings
        )

    async def test_reducer_ref_still_checked_without_registry(self) -> None:
        # Even without a ContractRegistry, reducer_ref validation still runs.
        validator = GraphValidator()
        graph = _attach_parallel_config(
            build_valid_graph(),
            ParallelConfig(
                split_path="items",
                merge_strategy="custom",
                reducer_ref="nonexistent_xyz.mod.fn",
            ),
        )
        report = await validator.validate(graph)
        codes = {i.code for i in report.issues}
        assert ValidationCode.INVALID_REDUCER_REF in codes


# =========================================================================
# Sanity: resolve_reducer_ref wraps ImportError cleanly
# =========================================================================


def test_resolve_reducer_ref_wraps_import_error() -> None:
    with pytest.raises(ReducerRefValidationError):
        from zeroth.core.parallel.reducers import resolve_reducer_ref

        resolve_reducer_ref("definitely_missing_mod.fn")
