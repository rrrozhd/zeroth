"""Merge the Zeroth governance handler into a run's callbacks without clobbering."""

from __future__ import annotations

from typing import Any

from langchain_core.callbacks import BaseCallbackHandler


def merge_governance_callbacks(
    config: Any,
    handler: BaseCallbackHandler,
) -> dict[str, Any]:
    """Return a ``RunnableConfig`` with ``handler`` appended to existing callbacks.

    The Zeroth handler is *added* to whatever the caller already supplied; it
    never replaces or duplicates user callbacks. Every shape ``config`` and
    ``config["callbacks"]`` can take is handled:

    * ``config`` absent or falsy: a fresh config carrying only the handler;
    * ``callbacks`` absent: ``[handler]``;
    * ``callbacks`` a list: ``[*existing, handler]`` (skipped if already present);
    * ``callbacks`` a ``BaseCallbackManager``: a copy with the handler added, so
      the caller's own manager is never mutated.

    Args:
        config: The user-provided runnable config mapping, or ``None``.
        handler: The Zeroth governance handler to register.

    Returns:
        A new config mapping. The input ``config`` is never mutated.
    """
    merged: dict[str, Any] = dict(config) if config else {}
    existing = merged.get("callbacks")

    if existing is None:
        merged["callbacks"] = [handler]
        return merged

    if isinstance(existing, list):
        if handler not in existing:
            merged["callbacks"] = [*existing, handler]
        return merged

    # Anything else is treated as a BaseCallbackManager-like object. Copy it so
    # the caller's manager is left untouched, then append if not already present.
    manager = existing.copy() if hasattr(existing, "copy") else existing
    handlers = getattr(manager, "handlers", ())
    if handler not in handlers and hasattr(manager, "add_handler"):
        manager.add_handler(handler, inherit=True)
    merged["callbacks"] = manager
    return merged


def inject_governance_handler(
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    handler: BaseCallbackHandler,
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Return ``(args, kwargs)`` with the governance handler merged into config.

    LangGraph entrypoints accept the runnable config either as the second
    positional argument or as a ``config=`` keyword (or omit it entirely). This
    locates whichever form the caller used, merges the Zeroth handler into it,
    and returns updated call arguments. Neither input is mutated.

    Args:
        args: Positional arguments the caller passed to the entrypoint.
        kwargs: Keyword arguments the caller passed to the entrypoint.
        handler: The Zeroth governance handler to register.

    Returns:
        The updated ``(args, kwargs)`` pair to forward to the wrapped graph.
    """
    if "config" in kwargs:
        merged = merge_governance_callbacks(kwargs["config"], handler)
        return args, {**kwargs, "config": merged}
    if len(args) >= 2:
        merged = merge_governance_callbacks(args[1], handler)
        return (args[0], merged, *args[2:]), kwargs
    return args, {**kwargs, "config": merge_governance_callbacks(None, handler)}
