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


# =========================================================================
# GraphRepository.publish() integration (Phase 43-02, D-15)
# =========================================================================


class TestPublishValidationHook:
    """Publish-path tests: validator is wired and called before save.

    Uses the real ``GraphRepository`` with the SQLite fixture to exercise
    the full DRAFT -> validate_or_raise -> save transition.
    """

    async def test_publish_with_valid_graph_succeeds(self, sqlite_db) -> None:
        from tests.graph.test_validation import build_valid_graph as build_graph
        from zeroth.core.graph.models import GraphStatus
        from zeroth.core.graph.repository import GraphRepository

        validator = GraphValidator()  # no registry — accepts plain graphs
        repository = GraphRepository(sqlite_db, validator=validator)
        graph = await repository.create(build_graph())
        published = await repository.publish(graph.graph_id, graph.version)
        assert published.status == GraphStatus.PUBLISHED

    async def test_publish_with_invalid_reducer_ref_fails_and_stays_draft(
        self, sqlite_db
    ) -> None:
        from tests.graph.test_validation import build_valid_graph as build_graph
        from zeroth.core.graph.models import GraphStatus
        from zeroth.core.graph.repository import GraphRepository

        validator = GraphValidator()
        repository = GraphRepository(sqlite_db, validator=validator)
        base = build_graph()
        # Attach a broken parallel_config to the first node before saving.
        nodes = list(base.nodes)
        nodes[0] = nodes[0].model_copy(
            update={
                "parallel_config": ParallelConfig(
                    split_path="items",
                    merge_strategy="custom",
                    reducer_ref="nonexistent_xyz.mod.fn",
                )
            }
        )
        broken = base.model_copy(update={"nodes": nodes})
        saved = await repository.save(broken)

        with pytest.raises(GraphValidationError):
            await repository.publish(saved.graph_id, saved.version)

        # Graph remains DRAFT — no partial state transition.
        reloaded = await repository.get(saved.graph_id, saved.version)
        assert reloaded is not None
        assert reloaded.status == GraphStatus.DRAFT

    async def test_publish_with_merge_array_contract_fails(self, sqlite_db) -> None:
        from tests.graph.test_validation import build_valid_graph as build_graph
        from zeroth.core.graph.models import GraphStatus
        from zeroth.core.graph.repository import GraphRepository

        registry = StubContractRegistry(
            {
                # Every contract_ref used by build_graph nodes maps to an
                # array-typed schema so the merge-strategy dict check fails.
            }
        )
        # Pre-populate by scanning the graph's node output_contract_refs.
        base = build_graph()
        for n in base.nodes:
            if n.output_contract_ref:
                registry._contracts[n.output_contract_ref] = {
                    "type": "array",
                    "items": {},
                }
        validator = GraphValidator(contract_registry=registry)  # type: ignore[arg-type]
        repository = GraphRepository(sqlite_db, validator=validator)
        nodes = list(base.nodes)
        nodes[0] = nodes[0].model_copy(
            update={
                "parallel_config": ParallelConfig(
                    split_path="items", merge_strategy="merge"
                )
            }
        )
        broken = base.model_copy(update={"nodes": nodes})
        saved = await repository.save(broken)

        with pytest.raises(GraphValidationError):
            await repository.publish(saved.graph_id, saved.version)

        reloaded = await repository.get(saved.graph_id, saved.version)
        assert reloaded is not None
        assert reloaded.status == GraphStatus.DRAFT

    async def test_publish_without_validator_still_works(self, sqlite_db) -> None:
        """Legacy construction path: no validator = no publish-time check."""
        from tests.graph.test_validation import build_valid_graph as build_graph
        from zeroth.core.graph.models import GraphStatus
        from zeroth.core.graph.repository import GraphRepository

        repository = GraphRepository(sqlite_db)  # no validator kwarg
        graph = await repository.create(build_graph())
        published = await repository.publish(graph.graph_id, graph.version)
        assert published.status == GraphStatus.PUBLISHED

    async def test_publish_calls_validator_exactly_once(self, sqlite_db) -> None:
        from tests.graph.test_validation import build_valid_graph as build_graph
        from zeroth.core.graph.repository import GraphRepository

        call_counter = {"count": 0}

        class SpyValidator(GraphValidator):
            async def validate_or_raise(self, graph):  # type: ignore[override]
                call_counter["count"] += 1
                return await super().validate_or_raise(graph)

        validator = SpyValidator()
        repository = GraphRepository(sqlite_db, validator=validator)
        graph = await repository.create(build_graph())
        await repository.publish(graph.graph_id, graph.version)
        assert call_counter["count"] == 1

    async def test_publish_no_parallel_config_backward_compat(
        self, sqlite_db
    ) -> None:
        """Graphs without any parallel_config publish normally."""
        from tests.graph.test_validation import build_valid_graph as build_graph
        from zeroth.core.graph.models import GraphStatus
        from zeroth.core.graph.repository import GraphRepository

        validator = GraphValidator()
        repository = GraphRepository(sqlite_db, validator=validator)
        graph = await repository.create(build_graph())
        published = await repository.publish(graph.graph_id, graph.version)
        assert published.status == GraphStatus.PUBLISHED

    async def test_retroactive_rejection_of_previously_unvalidated_draft(
        self, sqlite_db
    ) -> None:
        """A DRAFT saved without validation can now be rejected on re-publish.

        Documents the Phase 43-02 migration note: pre-existing DRAFT graphs
        with invalid parallel_configs will fail the new publish-time check.
        This is desirable — catches bugs — and is not auto-migrated.
        """
        from tests.graph.test_validation import build_valid_graph as build_graph
        from zeroth.core.graph.models import GraphStatus
        from zeroth.core.graph.repository import GraphRepository

        # Save via a validator-less repository (simulating pre-Phase-43 state).
        legacy_repo = GraphRepository(sqlite_db)
        base = build_graph()
        nodes = list(base.nodes)
        nodes[0] = nodes[0].model_copy(
            update={
                "parallel_config": ParallelConfig(
                    split_path="items",
                    merge_strategy="custom",
                    reducer_ref="nonexistent_xyz.mod.fn",
                )
            }
        )
        broken = base.model_copy(update={"nodes": nodes})
        saved = await legacy_repo.save(broken)
        assert saved.status == GraphStatus.DRAFT

        # Now publish through a wired repository — the new check fires.
        wired_repo = GraphRepository(sqlite_db, validator=GraphValidator())
        with pytest.raises(GraphValidationError):
            await wired_repo.publish(saved.graph_id, saved.version)
