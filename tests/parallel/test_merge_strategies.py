"""Unit tests for the fan-in merge-strategy registry (Phase 43-02).

Covers:
    * ``ParallelConfig`` model-validator enforcement of D-04 literal semantics.
    * Pure reducer functions (``_reduce_collect``, ``_reduce_merge``,
      ``_reduce_fold``) including None-skipping and non-dict errors.
    * ``resolve_reducer_ref`` regex guard, importlib failures, non-callable.
    * ``ParallelExecutor.collect_fan_in`` dispatching through the registry.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from pydantic import ValidationError

from zeroth.core.parallel.errors import (
    MergeStrategyError,
    ReducerRefValidationError,
)
from zeroth.core.parallel.executor import ParallelExecutor
from zeroth.core.parallel.models import BranchResult, ParallelConfig
from zeroth.core.parallel.reducers import (
    _default_fold,
    _reduce_collect,
    _reduce_fold,
    _reduce_merge,
    dispatch_strategy,
    resolve_reducer_ref,
)


# =========================================================================
# ParallelConfig validation (D-04 literal)
# =========================================================================


class TestParallelConfigValidation:
    def test_collect_default_is_backward_compatible(self) -> None:
        cfg = ParallelConfig(split_path="items")
        assert cfg.merge_strategy == "collect"
        assert cfg.reducer_ref is None

    def test_collect_with_explicit_strategy(self) -> None:
        cfg = ParallelConfig(split_path="items", merge_strategy="collect")
        assert cfg.merge_strategy == "collect"

    def test_reduce_without_reducer_ref_is_valid(self) -> None:
        # D-04 literal: `reduce` uses the built-in default fold and does NOT
        # require reducer_ref.
        cfg = ParallelConfig(split_path="items", merge_strategy="reduce")
        assert cfg.reducer_ref is None

    def test_reduce_with_reducer_ref_is_rejected(self) -> None:
        # reducer_ref is ONLY valid with merge_strategy='custom'.
        with pytest.raises(ValidationError, match="reducer_ref is only valid"):
            ParallelConfig(
                split_path="items",
                merge_strategy="reduce",
                reducer_ref="myapp.sum",
            )

    def test_merge_with_reducer_ref_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="reducer_ref is only valid"):
            ParallelConfig(
                split_path="items",
                merge_strategy="merge",
                reducer_ref="x.y",
            )

    def test_custom_without_reducer_ref_is_rejected(self) -> None:
        with pytest.raises(
            ValidationError, match="merge_strategy='custom' requires reducer_ref"
        ):
            ParallelConfig(split_path="items", merge_strategy="custom")

    def test_custom_with_reducer_ref_constructs(self) -> None:
        cfg = ParallelConfig(
            split_path="items",
            merge_strategy="custom",
            reducer_ref="tests._fixtures.reducers.sum_ints",
        )
        assert cfg.reducer_ref == "tests._fixtures.reducers.sum_ints"

    def test_merge_strategy_literal_rejects_unknown(self) -> None:
        with pytest.raises(ValidationError):
            ParallelConfig(split_path="items", merge_strategy="bogus")  # type: ignore[arg-type]


# =========================================================================
# Pure reducer functions
# =========================================================================


class TestReducers:
    def test_reduce_collect_preserves_order_and_none(self) -> None:
        outputs: list[dict | None] = [{"a": 1}, {"a": 2}, None]
        assert _reduce_collect(outputs) == [{"a": 1}, {"a": 2}, None]

    def test_reduce_merge_two_disjoint_dicts(self) -> None:
        assert _reduce_merge([{"a": 1}, {"b": 2}]) == {"a": 1, "b": 2}

    def test_reduce_merge_later_overwrites_earlier(self) -> None:
        # D-02: dict.update in branch-index order.
        assert _reduce_merge([{"a": 1}, {"a": 2}]) == {"a": 2}

    def test_reduce_merge_skips_none_branches(self) -> None:
        # D-19: None failed branches do not contribute.
        assert _reduce_merge([{"a": 1}, None, {"b": 2}]) == {"a": 1, "b": 2}

    def test_reduce_merge_on_non_dict_raises(self) -> None:
        with pytest.raises(MergeStrategyError, match="branch 1 produced str"):
            _reduce_merge([{"a": 1}, "not a dict"])  # type: ignore[list-item]

    def test_reduce_fold_three_branches(self) -> None:
        result = _reduce_fold(
            [{"v": 1}, {"v": 2}, {"v": 3}],
            reducer=lambda a, b: {"v": a["v"] + b["v"]},
        )
        assert result == {"v": 6}

    def test_reduce_fold_all_none(self) -> None:
        assert _reduce_fold([None, None, None], reducer=lambda a, b: a) is None

    def test_reduce_fold_single_element_no_call(self) -> None:
        calls: list[tuple] = []

        def spy(a: object, b: object) -> object:
            calls.append((a, b))
            return b

        assert _reduce_fold([{"v": 5}], reducer=spy) == {"v": 5}
        assert calls == []

    def test_reduce_fold_skips_none_mid_stream(self) -> None:
        result = _reduce_fold(
            [{"v": 1}, None, {"v": 2}],
            reducer=lambda a, b: {"v": a["v"] + b["v"]},
        )
        assert result == {"v": 3}

    def test_reduce_fold_wraps_reducer_exception(self) -> None:
        def bad(a: object, b: object) -> object:
            raise RuntimeError("boom")

        with pytest.raises(MergeStrategyError, match="reducer raised RuntimeError"):
            _reduce_fold([{"a": 1}, {"a": 2}], reducer=bad)

    def test_default_fold_is_last_wins(self) -> None:
        assert _default_fold({"v": 1}, {"v": 2}) == {"v": 2}


# =========================================================================
# resolve_reducer_ref import resolution + regex guard
# =========================================================================


class TestResolveReducerRef:
    def test_resolves_real_callable(self) -> None:
        fn = resolve_reducer_ref("tests._fixtures.reducers.sum_ints")
        assert fn(2, 3) == 5

    def test_resolves_stdlib_callable(self) -> None:
        fn = resolve_reducer_ref("math.floor")
        assert fn(1.9) == 1

    def test_nonexistent_module(self) -> None:
        with pytest.raises(
            ReducerRefValidationError, match="not importable"
        ):
            resolve_reducer_ref("nonexistent_xyz.module.fn")

    def test_missing_attribute(self) -> None:
        with pytest.raises(ReducerRefValidationError, match="not found in module"):
            resolve_reducer_ref("math.not_an_attr")

    def test_not_callable_attribute(self) -> None:
        with pytest.raises(ReducerRefValidationError, match="expected a callable"):
            resolve_reducer_ref("math.pi")

    def test_not_callable_fixture(self) -> None:
        with pytest.raises(ReducerRefValidationError, match="expected a callable"):
            resolve_reducer_ref("tests._fixtures.reducers.NOT_CALLABLE")

    def test_regex_rejects_spaces_before_importlib(self) -> None:
        with patch("importlib.import_module") as mock_import:
            with pytest.raises(
                ReducerRefValidationError, match="not a valid dotted"
            ):
                resolve_reducer_ref("bad path with spaces")
            mock_import.assert_not_called()

    def test_regex_rejects_single_segment(self) -> None:
        with patch("importlib.import_module") as mock_import:
            with pytest.raises(
                ReducerRefValidationError, match="not a valid dotted"
            ):
                resolve_reducer_ref("os")
            mock_import.assert_not_called()

    def test_regex_rejects_colon_separator(self) -> None:
        with patch("importlib.import_module") as mock_import:
            with pytest.raises(ReducerRefValidationError):
                resolve_reducer_ref("pkg.mod:fn")
            mock_import.assert_not_called()

    def test_regex_rejects_empty_string(self) -> None:
        with pytest.raises(ReducerRefValidationError):
            resolve_reducer_ref("")


# =========================================================================
# dispatch_strategy / collect_fan_in integration
# =========================================================================


def _make_results(outputs: list[dict | None]) -> list[BranchResult]:
    return [
        BranchResult(branch_index=i, output=out) for i, out in enumerate(outputs)
    ]


class TestDispatchStrategy:
    def test_collect_returns_list(self) -> None:
        result = dispatch_strategy("collect", [{"a": 1}, {"a": 2}])
        assert result == [{"a": 1}, {"a": 2}]

    def test_merge_returns_dict(self) -> None:
        result = dispatch_strategy("merge", [{"a": 1}, {"b": 2}])
        assert result == {"a": 1, "b": 2}

    def test_reduce_uses_default_fold(self) -> None:
        # D-04 literal: default fold is last-wins.
        result = dispatch_strategy("reduce", [{"v": 1}, {"v": 2}, {"v": 3}])
        assert result == {"v": 3}

    def test_custom_requires_reducer_ref_runtime_guard(self) -> None:
        with pytest.raises(MergeStrategyError, match="requires reducer_ref"):
            dispatch_strategy("custom", [{"v": 1}])

    def test_custom_with_fixture_reducer(self) -> None:
        result = dispatch_strategy(
            "custom",
            [1, 2, 3],  # type: ignore[list-item]
            reducer_ref="tests._fixtures.reducers.sum_ints",
        )
        assert result == 6

    def test_unknown_strategy_raises(self) -> None:
        with pytest.raises(MergeStrategyError, match="unknown merge_strategy"):
            dispatch_strategy("bogus", [{"a": 1}])


class TestCollectFanInDispatch:
    def test_collect_backward_compat(self) -> None:
        executor = ParallelExecutor()
        config = ParallelConfig(split_path="items", merge_strategy="collect")
        results = _make_results([{"x": 1}, {"x": 2}])
        fan_in = executor.collect_fan_in(results, config, {"items": []})
        # Backward compat: collect writes a list at split_path.
        assert fan_in.merged_output["items"] == [{"x": 1}, {"x": 2}]

    def test_merge_writes_dict_at_split_path(self) -> None:
        executor = ParallelExecutor()
        config = ParallelConfig(split_path="result", merge_strategy="merge")
        results = _make_results([{"a": 1}, {"b": 2}])
        fan_in = executor.collect_fan_in(results, config, {})
        assert fan_in.merged_output["result"] == {"a": 1, "b": 2}

    def test_reduce_writes_default_fold_value(self) -> None:
        executor = ParallelExecutor()
        config = ParallelConfig(split_path="result", merge_strategy="reduce")
        results = _make_results([{"v": 1}, {"v": 2}, {"v": 3}])
        fan_in = executor.collect_fan_in(results, config, {})
        # Default last-wins fold.
        assert fan_in.merged_output["result"] == {"v": 3}

    def test_custom_writes_reducer_value(self) -> None:
        executor = ParallelExecutor()
        config = ParallelConfig(
            split_path="result",
            merge_strategy="custom",
            reducer_ref="tests._fixtures.reducers.sum_scores",
        )
        results = _make_results([{"total": 10}, {"total": 20}, {"total": 5}])
        fan_in = executor.collect_fan_in(results, config, {})
        assert fan_in.merged_output["result"] == {"total": 35}
