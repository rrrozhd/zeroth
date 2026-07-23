"""The governed LangGraph wrapper: transparent, observed-mode delegation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.runnables.config import merge_configs

from zeroth.integrations.langgraph._callbacks import inject_governance_handler
from zeroth.integrations.langgraph._handler import ZerothGovernanceCallbackHandler

_COMPOSE_ERROR = (
    "GovernedGraph does not support `|` composition; compose the graph before "
    "governing it (e.g. govern_graph(a | b)), then invoke the governed wrapper."
)


def _apply_bound_config(
    args: tuple[Any, ...], kwargs: dict[str, Any], bound: Any
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Merge the ``with_config`` bound config *under* the call-time config.

    Mirrors ``RunnableBinding``: the bound config is the base and the call-time
    config layers on top (``merge_configs(bound, call)``), so a governed
    ``with_config(...)`` run is equivalent to the same bind on the bare graph.
    The config is located wherever the caller placed it (``config=`` keyword,
    second positional, or absent). Neither input is mutated.
    """
    if "config" in kwargs:
        return args, {**kwargs, "config": merge_configs(bound, kwargs["config"])}
    if len(args) >= 2:
        return (args[0], merge_configs(bound, args[1]), *args[2:]), kwargs
    return args, {**kwargs, "config": merge_configs(bound, None)}


@dataclass(frozen=True)
class RunStartContext:
    """Attestation-free context handed to an ``on_run_start`` stability hook.

    Surface stability only. It exposes the wrapped graph's identity and the fact
    that the Zeroth governance handler is registered for the run. It deliberately
    carries **no** governance level and **no** attestation payload; ZER-2 mints
    neither.

    Attributes:
        graph: The wrapped compiled graph (identity only).
        handler_registered: Always ``True`` when emitted; the handler is merged
            before the hook fires.
        entrypoint: Which entrypoint started the run: ``"invoke"``, ``"ainvoke"``,
            ``"stream"`` or ``"astream"``.
    """

    graph: Any
    handler_registered: bool
    entrypoint: str


type OnRunStart = Callable[[RunStartContext], None]


class GovernedGraph:
    """Transparent, observed-mode governance wrapper around a compiled LangGraph.

    ``GovernedGraph`` reuses the econ instrumentation delegation
    (``InstrumentedLangGraph``) so cost capture keeps working, and merges a
    Zeroth governance callback handler into each run's ``RunnableConfig`` without
    replacing or duplicating any user-supplied callbacks.

    It is deliberately behaviour-preserving: results, streamed chunks and
    propagated exceptions are identical to calling the wrapped graph directly.
    It mints no attestation and introduces no path that promotes a run's reported
    governance level above ``admission`` (FA5 / ZER-2); promotion to ``observed``
    is deferred. Unknown attributes delegate to the wrapped graph via
    :meth:`__getattr__`.
    """

    _RESERVED = frozenset({"_graph", "_on_run_start", "_handler", "_delegate", "_bound_config"})

    def __init__(
        self,
        graph: Any,
        *,
        on_run_start: OnRunStart | None = None,
        bound_config: Any = None,
    ) -> None:
        """Wrap ``graph``; optionally register a one-shot ``on_run_start`` hook.

        Args:
            graph: A compiled LangGraph (anything exposing the Runnable
                ``invoke`` / ``ainvoke`` / ``stream`` / ``astream`` surface).
            on_run_start: Optional stability seam invoked exactly once per
                run-start. Defaults to ``None`` (no-op).
            bound_config: Config bound via :meth:`with_config`, merged under every
                run's call-time config. Internal; callers use ``with_config``.
        """
        # Imported lazily so importing this package never requires ``langgraph``
        # and never eagerly constructs the econ runtime and its transport.
        from zeroth.econ.instrumentation.integrations.langgraph import (
            instrument_langgraph_graph,
        )

        self._graph = graph
        self._on_run_start = on_run_start
        self._bound_config = bound_config
        self._handler = ZerothGovernanceCallbackHandler()
        # Reuse -- not reimplement -- the econ delegation shape.
        self._delegate = instrument_langgraph_graph(
            graph,
            capability_id="zeroth.integrations.langgraph",
            implementation_id=self._graph_identity(graph),
        )

    @staticmethod
    def _graph_identity(graph: Any) -> str:
        """Return a stable identity string for ``graph``."""
        name = getattr(graph, "name", None)
        return str(name) if name else type(graph).__name__

    def _run_start(self, entrypoint: str) -> None:
        """Fire the ``on_run_start`` hook once, if one was registered."""
        if self._on_run_start is None:
            return
        self._on_run_start(
            RunStartContext(
                graph=self._graph,
                handler_registered=True,
                entrypoint=entrypoint,
            )
        )

    def _prepare(
        self, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> tuple[tuple[Any, ...], dict[str, Any]]:
        """Bind any :meth:`with_config` config, then merge the governance handler."""
        if self._bound_config:
            args, kwargs = _apply_bound_config(args, kwargs, self._bound_config)
        return inject_governance_handler(args, kwargs, self._handler)

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        """Invoke the wrapped graph with the governance handler merged in."""
        args, kwargs = self._prepare(args, kwargs)
        self._run_start("invoke")
        return self._delegate.invoke(*args, **kwargs)

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        """Async-invoke the wrapped graph with the governance handler merged in."""
        args, kwargs = self._prepare(args, kwargs)
        self._run_start("ainvoke")
        return await self._delegate.ainvoke(*args, **kwargs)

    def stream(self, *args: Any, **kwargs: Any) -> Any:
        """Stream the wrapped graph with the governance handler merged in."""
        args, kwargs = self._prepare(args, kwargs)
        self._run_start("stream")
        return self._delegate.stream(*args, **kwargs)

    def astream(self, *args: Any, **kwargs: Any) -> Any:
        """Async-stream the wrapped graph with the governance handler merged in."""
        args, kwargs = self._prepare(args, kwargs)
        self._run_start("astream")
        return self._delegate.astream(*args, **kwargs)

    def with_config(self, config: Any = None, **kwargs: Any) -> GovernedGraph:
        """Return a still-governed graph that binds ``config`` into every run.

        Mirrors ``Runnable.with_config`` but keeps governance intact. Delegating
        to the wrapped graph would hand back a bare, ungoverned ``RunnableBinding``
        (governance silently dropped); instead this returns a new
        :class:`GovernedGraph` around the *same* underlying graph, carrying the
        bound config, the ``on_run_start`` seam and a governance handler. Chained
        ``with_config`` calls shallow-overwrite previously bound top-level keys
        (tags/metadata/configurable/callbacks/run_name/...) wholesale, matching
        ``RunnableBinding``; they do not accumulate. Attribute delegation and
        single-handler injection are preserved, and the bound config is applied to
        every ``invoke`` / ``ainvoke`` / ``stream`` / ``astream`` call exactly as
        the bare graph's ``with_config`` would.

        Args:
            config: A ``RunnableConfig`` mapping to bind. Defaults to ``None``.
            **kwargs: Individual config fields, mirroring ``Runnable.with_config``.

        Returns:
            A new :class:`GovernedGraph` wrapping the same underlying graph with
            the merged config bound.
        """
        call_config = {**(config or {}), **kwargs}
        # Shallow top-level overwrite, matching ``RunnableBinding.with_config``:
        # each present key in ``call_config`` REPLACES the previously bound value
        # wholesale rather than accumulating it. ``merge_configs`` is applied only
        # at invoke time (``_apply_bound_config``) -- exactly where a
        # ``RunnableBinding`` layers its bound config under the call-time config.
        new_bound = {**(self._bound_config or {}), **call_config}
        return GovernedGraph(self._graph, on_run_start=self._on_run_start, bound_config=new_bound)

    def __or__(self, _other: Any) -> Any:
        """Reject pipe composition; ZER-2 governs graphs, it does not compose them."""
        raise TypeError(_COMPOSE_ERROR)

    def __ror__(self, _other: Any) -> Any:
        """Reject reflected pipe composition with the same actionable error."""
        raise TypeError(_COMPOSE_ERROR)

    def __getattr__(self, item: str) -> Any:
        """Delegate unknown attributes to the econ delegate (and thus the graph)."""
        # Guard against recursion if accessed before ``__init__`` finishes.
        if item in GovernedGraph._RESERVED:
            raise AttributeError(item)
        return getattr(self._delegate, item)


def govern_graph(graph: Any, *, on_run_start: OnRunStart | None = None) -> GovernedGraph:
    """Install one-line, observed-mode governance over a compiled LangGraph.

    The returned :class:`GovernedGraph` is a transparent wrapper: ``invoke``,
    ``ainvoke``, ``stream`` and ``astream`` return byte-for-byte equivalent
    results and identically ordered chunks, propagate upstream exceptions
    unchanged, and delegate every other attribute to the wrapped graph. Cost
    capture keeps working because the wrapper reuses the econ instrumentation
    delegation, and a Zeroth governance callback handler is merged into each
    run's ``config["callbacks"]`` without replacing or duplicating any callbacks
    the caller already registered.

    ZER-2 is observed-mode groundwork only: the wrapper mints no attestation and
    introduces no path that promotes a run's reported governance level above
    ``admission`` (FA5). Promotion to ``observed`` is deferred.

    Args:
        graph: A compiled LangGraph (anything exposing ``invoke`` / ``ainvoke`` /
            ``stream`` / ``astream``).
        on_run_start: Optional stability seam invoked exactly once per run-start
            (per ``invoke`` / ``ainvoke`` / ``stream`` / ``astream`` call) with a
            small, attestation-free :class:`RunStartContext`. Defaults to ``None``
            (no-op), in which case output is byte-identical to omitting it.

    Returns:
        A :class:`GovernedGraph` wrapping ``graph``.

    Example:
        >>> from zeroth.integrations.langgraph import govern_graph
        >>> graph = govern_graph(compiled_graph)
        >>> graph.invoke({"messages": [...]})
    """
    return GovernedGraph(graph, on_run_start=on_run_start)
