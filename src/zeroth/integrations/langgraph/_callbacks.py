"""Merge the Zeroth governance handler into a run's callbacks without clobbering."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

from zeroth.integrations.langgraph._handler import ZerothGovernanceCallbackHandler


def _has_governance_handler(handlers: Iterable[Any]) -> bool:
    """Return ``True`` if a Zeroth governance handler is already present.

    Detection is by **type** (``isinstance``), never by equality: a user callback
    whose ``__eq__`` returns ``True`` for everything must not be able to
    masquerade as -- and thereby suppress -- the governance handler, and two
    distinct governance-handler instances (e.g. from ``govern_graph`` nesting)
    must be recognised as the same governance role so only one is ever installed.
    """
    return any(isinstance(h, ZerothGovernanceCallbackHandler) for h in handlers)


def _find_governance_handler(handler_lists: Iterable[Any]) -> Any:
    """Return the first governance-handler *instance* found across ``handler_lists``.

    Detection is by ``isinstance`` so a pre-installed governance handler is reused
    (its identity shared into every list) rather than duplicated. Returns ``None``
    when no list carries a governance handler.
    """
    for handlers in handler_lists:
        for h in handlers:
            if isinstance(h, ZerothGovernanceCallbackHandler):
                return h
    return None


def _register_in_manager(manager: Any, handler: BaseCallbackHandler) -> None:
    """Ensure exactly one governance handler lives in BOTH manager handler lists.

    Governance must be inheritable, so it belongs in ``handlers`` *and*
    ``inheritable_handlers`` (langchain's ``add_handler(h, inherit=True)`` intent).
    ``add_handler`` is bypassed because its ``handler not in self.handlers``
    dedup is an equality test a hostile user ``__eq__`` could exploit to suppress
    governance. Instead, reuse any governance instance already present in either
    list (by ``isinstance`` -- so the *same* identity lands in both) or the
    wrapper's own handler, then normalise each list to carry exactly that one
    canonical identity (stripping any divergent or duplicate governance
    instances). Manager-likes exposing neither list fall back to ``add_handler``.
    """
    lists = [
        seq
        for seq in (
            getattr(manager, "handlers", None),
            getattr(manager, "inheritable_handlers", None),
        )
        if isinstance(seq, list)
    ]
    if not lists:
        if hasattr(manager, "add_handler"):
            manager.add_handler(handler, inherit=True)
        return
    governance = _find_governance_handler(lists) or handler
    for handlers in lists:
        # Normalise each list: strip every governance instance (collapsing
        # divergent identities across the two lists and any pre-existing
        # duplicates) and re-add exactly the one canonical handler, so both lists
        # end up carrying the same single governance identity.
        handlers[:] = [h for h in handlers if not isinstance(h, ZerothGovernanceCallbackHandler)]
        handlers.append(governance)


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
    * ``callbacks`` a list: ``[*existing, handler]`` (skipped only if a
      governance handler -- recognised by type -- is already present);
    * ``callbacks`` a ``BaseCallbackManager``: a copy carrying exactly one
      governance handler in both ``handlers`` and ``inheritable_handlers`` (so it
      propagates to child runs), leaving the caller's own manager untouched.

    Whether the handler is already present is decided solely by governance-handler
    *type* / *identity*; arbitrary user-callback equality never gets a say, so a
    hostile ``__eq__`` can neither suppress nor duplicate governance.

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
        if not _has_governance_handler(existing):
            merged["callbacks"] = [*existing, handler]
        return merged

    # Anything else is treated as a BaseCallbackManager-like object. Copy it so
    # the caller's manager is left untouched, then ensure exactly one governance
    # handler in each of its handler lists (see _register_in_manager).
    manager = existing.copy() if hasattr(existing, "copy") else existing
    _register_in_manager(manager, handler)
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
