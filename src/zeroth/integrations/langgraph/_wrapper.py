"""The governed LangGraph wrapper: transparent, observed-mode delegation."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from langchain_core.runnables.config import merge_configs

from zeroth.integrations.langgraph._callbacks import inject_governance_handler
from zeroth.integrations.langgraph._correlation import (
    correlation_from_call,
    reserved_token_from_call,
    reset_correlation,
    reset_reserved_context_token,
    set_correlation,
    set_reserved_context_token,
)
from zeroth.integrations.langgraph._handler import ZerothGovernanceCallbackHandler

if TYPE_CHECKING:
    from zeroth.integrations.langgraph._gateway_client import LangGraphGatewayClient

_COMPOSE_ERROR = (
    "GovernedGraph does not support `|` composition; compose the graph before "
    "governing it (e.g. govern_graph(a | b)), then invoke the governed wrapper."
)
_RESUME_LATEST_CHECKPOINT = "_zeroth_resume_latest_checkpoint"
_RESUME_LATEST_CHECKPOINT_CAPABILITY = object()


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


def _consume_latest_checkpoint_marker(
    args: tuple[Any, ...], kwargs: dict[str, Any]
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Discard a bound replay checkpoint for an approval resume."""

    def cleaned(config: Any) -> Any:
        if not isinstance(config, Mapping):
            return config
        configurable = config.get("configurable")
        if (
            not isinstance(configurable, Mapping)
            or configurable.get(_RESUME_LATEST_CHECKPOINT)
            is not _RESUME_LATEST_CHECKPOINT_CAPABILITY
        ):
            return config
        current = dict(configurable)
        current.pop(_RESUME_LATEST_CHECKPOINT)
        for key in ("checkpoint_id", "checkpoint_ns", "checkpoint_map"):
            current.pop(key, None)
        return {**config, "configurable": current}

    if "config" in kwargs:
        return args, {**kwargs, "config": cleaned(kwargs["config"])}
    if len(args) >= 2:
        return (args[0], cleaned(args[1]), *args[2:]), kwargs
    return args, kwargs


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
            ``"stream"``, ``"astream"`` or ``"astream_events"``.
    """

    graph: Any
    handler_registered: bool
    entrypoint: str


type OnRunStart = Callable[[RunStartContext], None]


@contextmanager
def _published_run_context(
    correlation_id: str | None, reserved_token: str | None
) -> Iterator[None]:
    correlation_marker = set_correlation(correlation_id)
    token_marker = set_reserved_context_token(reserved_token)
    try:
        yield
    finally:
        reset_reserved_context_token(token_marker)
        reset_correlation(correlation_marker)


class _ContextualAsyncIterator:
    """Keep private run context around a v3 driver's pulls and close."""

    def __init__(
        self,
        iterator: Any,
        correlation: str | None,
        reserved_token: str | None,
    ) -> None:
        self._iterator = iterator
        self._correlation = correlation
        self._reserved_token = reserved_token

    def __aiter__(self) -> _ContextualAsyncIterator:
        return self

    async def __anext__(self) -> Any:
        with _published_run_context(self._correlation, self._reserved_token):
            return await self._iterator.__anext__()

    async def aclose(self) -> None:
        aclose = getattr(self._iterator, "aclose", None)
        if aclose is not None:
            with _published_run_context(self._correlation, self._reserved_token):
                await aclose()


class GovernedGraph:
    """Transparent, observed-mode governance wrapper around a compiled LangGraph.

    ``GovernedGraph`` reuses the econ instrumentation delegation
    (``InstrumentedLangGraph``) so cost capture keeps working, and merges a
    Zeroth governance callback handler into each run's ``RunnableConfig`` without
    replacing or duplicating any user-supplied callbacks.

    Without a gateway client it is behaviour-preserving: results, streamed chunks
    and propagated exceptions are identical to calling the wrapped graph directly.
    With a gateway client it fails closed until inventory registration and a
    server-authoritative run-start attestation succeed. Unknown attributes
    delegate to the wrapped graph via :meth:`__getattr__`.
    """

    _RESERVED = frozenset(
        {
            "_graph",
            "_on_run_start",
            "_gateway_client",
            "_handler",
            "_delegate",
            "_bound_config",
        }
    )

    def __init__(
        self,
        graph: Any,
        *,
        on_run_start: OnRunStart | None = None,
        gateway_client: LangGraphGatewayClient | None = None,
        bound_config: Any = None,
    ) -> None:
        """Wrap ``graph``; optionally register a one-shot ``on_run_start`` hook.

        Args:
            graph: A compiled LangGraph exposing the governed runnable
                entrypoints.
            on_run_start: Optional stability seam invoked exactly once per
                run-start. Defaults to ``None`` (no-op).
            gateway_client: Optional enforcement client that registers inventory
                and attests before the graph or caller hook executes.
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
        self._gateway_client = gateway_client
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

    def _run_start(
        self,
        entrypoint: str,
        correlation_id: str | None,
        reserved_token: str | None,
    ) -> None:
        """Attest before any caller hook or wrapped graph code can execute."""
        if self._gateway_client is not None:
            if not correlation_id or not reserved_token:
                from zeroth.integrations.langgraph._gateway_client import LangGraphGatewayError

                raise LangGraphGatewayError()
            self._gateway_client.start_run(reserved_token, correlation_id)
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
    ) -> tuple[tuple[Any, ...], dict[str, Any], str | None, str | None]:
        """Bind config, extract the correlation id, then merge the governance handler.

        Correlation is extracted after any :meth:`with_config` bind (so a token in
        the bound config is seen); the extracted id is *returned* for the caller
        to publish on the wrapper-owned ``ContextVar`` around the delegate run
        (see :func:`set_correlation`). It is never written into the run config, so
        no caller-reachable metadata channel carries it. Purely observational: it
        leaves results / order / cancellation unchanged.
        """
        if self._bound_config:
            args, kwargs = _apply_bound_config(args, kwargs, self._bound_config)
        args, kwargs = _consume_latest_checkpoint_marker(args, kwargs)
        correlation = correlation_from_call(args, kwargs)
        reserved_token = reserved_token_from_call(args, kwargs)
        args, kwargs = inject_governance_handler(args, kwargs, self._handler)
        return args, kwargs, correlation, reserved_token

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        """Invoke the wrapped graph with the governance handler merged in."""
        args, kwargs, correlation, reserved_token = self._prepare(args, kwargs)
        self._run_start("invoke", correlation, reserved_token)
        with _published_run_context(correlation, reserved_token):
            return self._delegate.invoke(*args, **kwargs)

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        """Async-invoke the wrapped graph with the governance handler merged in."""
        args, kwargs, correlation, reserved_token = self._prepare(args, kwargs)
        self._run_start("ainvoke", correlation, reserved_token)
        with _published_run_context(correlation, reserved_token):
            return await self._delegate.ainvoke(*args, **kwargs)

    def stream(self, *args: Any, **kwargs: Any) -> Any:
        """Stream the wrapped graph with the governance handler merged in.

        The delegate generator is iterated by the caller *after* this method
        returns, so the correlation ``ContextVar`` is published inside the wrapper
        generator -- set when iteration begins, reset when it ends -- not in the
        method body (which would reset it before the caller ever iterates).
        """
        args, kwargs, correlation, reserved_token = self._prepare(args, kwargs)
        self._run_start("stream", correlation, reserved_token)
        return self._correlated_stream(args, kwargs, correlation, reserved_token)

    def astream(self, *args: Any, **kwargs: Any) -> Any:
        """Async-stream the wrapped graph with the governance handler merged in.

        As with :meth:`stream`, the async generator is driven after this method
        returns, so the correlation ``ContextVar`` is published inside the wrapper
        async generator (set at first iteration, reset in ``finally``), never in
        the method body. Stays a plain ``def`` returning an async iterator -- an
        ``async def`` here would return a coroutine callers cannot ``async for``.
        """
        args, kwargs, correlation, reserved_token = self._prepare(args, kwargs)
        self._run_start("astream", correlation, reserved_token)
        return self._correlated_astream("astream", args, kwargs, correlation, reserved_token)

    def astream_events(self, *args: Any, **kwargs: Any) -> Any:
        """Stream events with governance and private per-event run context."""
        args, kwargs, correlation, reserved_token = self._prepare(args, kwargs)
        self._run_start("astream_events", correlation, reserved_token)
        if kwargs.get("version") == "v3":
            return self._correlated_astream_events_v3(args, kwargs, correlation, reserved_token)
        return self._correlated_astream("astream_events", args, kwargs, correlation, reserved_token)

    def batch(
        self,
        inputs: list[Any],
        config: Any = None,
        *,
        return_exceptions: bool = False,
        **kwargs: Any,
    ) -> list[Any]:
        """Invoke every input through the governed sync entrypoint."""
        from langchain_core.runnables import Runnable

        return Runnable.batch(
            self,
            inputs,
            config,
            return_exceptions=return_exceptions,
            **kwargs,
        )

    async def abatch(
        self,
        inputs: list[Any],
        config: Any = None,
        *,
        return_exceptions: bool = False,
        **kwargs: Any,
    ) -> list[Any]:
        """Async-invoke every input through the governed async entrypoint."""
        from langchain_core.runnables import Runnable

        return await Runnable.abatch(
            self,
            inputs,
            config,
            return_exceptions=return_exceptions,
            **kwargs,
        )

    def _correlated_stream(
        self,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        correlation: str | None,
        reserved_token: str | None,
    ) -> Any:
        """Yield the delegate stream with the correlation published for its duration.

        A generator: its body runs only when the caller iterates. The ``ContextVar``
        is set around each individual ``next()`` -- so every node callback, including
        those on parallel worker threads that inherit this context, reads it -- and
        reset *before* the chunk is yielded. Scoping it per-chunk (rather than across
        the yields) keeps the correlation out of the consumer's context, leaks nothing
        when an iterator is abandoned, stops interleaved streams observing each other,
        and keeps every token confined to the context that created it. Chunk order and
        laziness are unchanged.
        """
        iterator = iter(self._delegate.stream(*args, **kwargs))
        try:
            while True:
                try:
                    with _published_run_context(correlation, reserved_token):
                        chunk = next(iterator)
                except StopIteration:
                    return
                yield chunk
        finally:
            close = getattr(iterator, "close", None)
            if close is not None:
                with _published_run_context(correlation, reserved_token):
                    close()

    async def _correlated_astream(
        self,
        entrypoint: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        correlation: str | None,
        reserved_token: str | None,
    ) -> Any:
        """Async-yield the delegate stream with private run context."""
        iterator = getattr(self._delegate, entrypoint)(*args, **kwargs).__aiter__()
        try:
            while True:
                with _published_run_context(correlation, reserved_token):
                    try:
                        chunk = await iterator.__anext__()
                    except StopAsyncIteration:
                        return
                yield chunk
        finally:
            aclose = getattr(iterator, "aclose", None)
            if aclose is not None:
                with _published_run_context(correlation, reserved_token):
                    await aclose()

    async def _correlated_astream_events_v3(
        self,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        correlation: str | None,
        reserved_token: str | None,
    ) -> Any:
        """Await a v3 run and scope every pull of its caller-driven iterator."""
        with _published_run_context(correlation, reserved_token):
            run = await self._delegate.astream_events(*args, **kwargs)
        run._graph_aiter = _ContextualAsyncIterator(
            run._graph_aiter,
            correlation,
            reserved_token,
        )
        return run

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
        every governed entrypoint call exactly as the bare graph's
        ``with_config`` would.

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
        return GovernedGraph(
            self._graph,
            on_run_start=self._on_run_start,
            gateway_client=self._gateway_client,
            bound_config=new_bound,
        )

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


def govern_graph(
    graph: Any,
    *,
    on_run_start: OnRunStart | None = None,
    gateway_client: LangGraphGatewayClient | None = None,
) -> GovernedGraph:
    """Install one-line, observed-mode governance over a compiled LangGraph.

    The returned :class:`GovernedGraph` is a transparent wrapper: ``invoke``,
    ``ainvoke``, ``stream``, ``astream``, ``astream_events``, ``batch`` and
    ``abatch`` return equivalent results and identically ordered chunks/events,
    propagate upstream exceptions unchanged, and delegate every other attribute
    to the wrapped graph. Cost
    capture keeps working because the wrapper reuses the econ instrumentation
    delegation, and a Zeroth governance callback handler is merged into each
    run's ``config["callbacks"]`` without replacing or duplicating any callbacks
    the caller already registered.

    Without ``gateway_client`` this remains transparent observed-mode groundwork.
    With one, inventory registration and a server-authoritative run-start
    attestation must succeed before the wrapped graph can execute.

    Args:
        graph: A compiled LangGraph exposing the governed runnable entrypoints.
        on_run_start: Optional stability seam invoked exactly once per run-start
            (once per stream, or once per batch input) with a small,
            attestation-free :class:`RunStartContext`. Defaults to ``None``
            (no-op).
        gateway_client: Optional enforcement client. When supplied, a valid
            gateway reserved token is required and run-start evidence is emitted
            before delegation.

    Returns:
        A :class:`GovernedGraph` wrapping ``graph``.

    Example:
        >>> from zeroth.integrations.langgraph import govern_graph
        >>> graph = govern_graph(compiled_graph)
        >>> graph.invoke({"messages": [...]})
    """
    return GovernedGraph(
        graph,
        on_run_start=on_run_start,
        gateway_client=gateway_client,
    )
