"""Fan-in reduction strategies for ParallelExecutor.

Strategies (D-04 literal):
    * ``collect`` - return outputs as-is (list preserving branch order, None preserved).
    * ``merge``   - shallow dict merge in branch-index order via ``dict.update`` (D-02).
    * ``reduce``  - sequential left-to-right fold with BUILT-IN DEFAULT reducer
      (``_default_fold``, last-wins). No ``reducer_ref`` required (D-01 + D-04).
    * ``custom``  - sequential left-to-right fold with USER-SUPPLIED reducer resolved
      from ``reducer_ref`` via ``importlib`` (requires ``reducer_ref`` per D-04).

Reducer contract (D-01):
    ``ReducerFn = Callable[[Any, Any], Any]``
    Receives ``(accumulator, next_value)``; returns new accumulator. Initial
    accumulator is the first non-None branch output in index order. 2-arg only.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Callable
from typing import Any

from zeroth.core.parallel.errors import (
    MergeStrategyError,
    ReducerRefValidationError,
)

ReducerFn = Callable[[Any, Any], Any]
StrategyFn = Callable[..., Any]

# D-16 + threat mitigation T-43-02-01: only accept dotted identifiers like
# ``pkg.mod.fn`` or ``pkg.mod.sub.fn``. At least one dot required so
# single-segment module names like ``os`` are rejected (prevents accidental
# top-level stdlib imports). Rejected BEFORE any importlib call.
_REDUCER_REF_PATTERN = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+$"
)


def _reduce_collect(
    outputs: list[dict[str, Any] | None],
) -> list[dict[str, Any] | None]:
    """Return outputs unchanged (branch-index order, None preserved per D-19)."""
    return list(outputs)


def _reduce_merge(
    outputs: list[dict[str, Any] | None],
) -> dict[str, Any]:
    """Shallow dict merge in branch-index order (D-02).

    Later branches overwrite earlier keys via ``dict.update``. ``None``
    branches are skipped (D-19). Non-dict outputs raise ``MergeStrategyError``.
    """
    merged: dict[str, Any] = {}
    for i, out in enumerate(outputs):
        if out is None:
            continue
        if not isinstance(out, dict):
            raise MergeStrategyError(
                f"merge strategy requires dict outputs, branch {i} produced "
                f"{type(out).__name__}"
            )
        merged.update(out)
    return merged


def _reduce_fold(
    outputs: list[dict[str, Any] | None],
    *,
    reducer: ReducerFn,
) -> Any:
    """Sequential left-to-right fold with a 2-arg reducer (D-01).

    Skips ``None`` branches (D-19). Returns ``None`` if every branch is None.
    Returns the single element if only one non-None branch (no fold needed).
    """
    non_null = [o for o in outputs if o is not None]
    if not non_null:
        return None
    if len(non_null) == 1:
        return non_null[0]
    acc: Any = non_null[0]
    for nxt in non_null[1:]:
        try:
            acc = reducer(acc, nxt)
        except MergeStrategyError:
            raise
        except Exception as exc:  # noqa: BLE001 - wrap any reducer failure
            raise MergeStrategyError(
                f"reducer raised {type(exc).__name__}: {exc}"
            ) from exc
    return acc


def resolve_reducer_ref(reducer_ref: str) -> ReducerFn:
    """Resolve a dotted import path to a callable.

    Security (threat T-43-02-01): only strings matching ``_REDUCER_REF_PATTERN``
    are passed to ``importlib.import_module``. Strings with spaces, colons,
    relative imports, or single-segment module names are rejected BEFORE any
    code runs. Graph authors are trusted (publish requires authentication);
    this is defense in depth.

    Raises ``ReducerRefValidationError`` on any failure.
    """
    if not isinstance(reducer_ref, str) or not _REDUCER_REF_PATTERN.match(reducer_ref):
        raise ReducerRefValidationError(
            f"reducer_ref {reducer_ref!r} is not a valid dotted import path; "
            "expected pattern: module.submodule.function"
        )
    module_path, _, attr = reducer_ref.rpartition(".")
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise ReducerRefValidationError(
            f"reducer module {module_path!r} not importable: {exc}"
        ) from exc
    if not hasattr(module, attr):
        raise ReducerRefValidationError(
            f"reducer {attr!r} not found in module {module_path!r}"
        )
    fn = getattr(module, attr)
    if not callable(fn):
        raise ReducerRefValidationError(
            f"reducer_ref {reducer_ref!r} resolved to {type(fn).__name__}, "
            "expected a callable"
        )
    return fn


def _default_fold(acc: Any, nxt: Any) -> Any:
    """Built-in default reducer for ``merge_strategy='reduce'`` (D-04 literal).

    Trivial last-wins fold: returns the next value, discarding the
    accumulator. Satisfies D-01 (a sequential left-to-right fold from branch
    0 exists and runs) and gives ``reduce`` a distinct identity from
    ``custom`` (reduce = platform default; custom = user-supplied dotted
    path). For non-trivial reductions, author must use ``merge_strategy='custom'``
    with an explicit ``reducer_ref``.

    A named function (not a lambda) so tracebacks are traceable.
    """
    return nxt


_STRATEGY_REGISTRY: dict[str, StrategyFn] = {
    "collect": _reduce_collect,
    "merge": _reduce_merge,
    # "reduce" dispatches to _reduce_fold(outputs, reducer=_default_fold)
    # "custom" dispatches to _reduce_fold(outputs, reducer=resolve_reducer_ref(ref))
}


def dispatch_strategy(
    strategy: str,
    outputs: list[dict[str, Any] | None],
    *,
    reducer_ref: str | None = None,
) -> Any:
    """Unified entry point for ``collect_fan_in``.

    Dispatches to the right handler based on ``strategy``. Per D-04 literal:

    * ``reduce`` uses ``_default_fold`` when ``reducer_ref is None``. The
      ``ParallelConfig`` model validator rejects ``reduce`` with a non-None
      ``reducer_ref`` before reaching this dispatch.
    * ``custom`` always requires a non-None ``reducer_ref``; resolved via
      ``resolve_reducer_ref``.
    """
    if strategy == "reduce":
        return _reduce_fold(outputs, reducer=_default_fold)
    if strategy == "custom":
        if reducer_ref is None:
            # Defense in depth — model validator should have caught this.
            raise MergeStrategyError(
                "merge_strategy='custom' requires reducer_ref"
            )
        reducer = resolve_reducer_ref(reducer_ref)
        return _reduce_fold(outputs, reducer=reducer)
    handler = _STRATEGY_REGISTRY.get(strategy)
    if handler is None:
        raise MergeStrategyError(f"unknown merge_strategy {strategy!r}")
    return handler(outputs)
