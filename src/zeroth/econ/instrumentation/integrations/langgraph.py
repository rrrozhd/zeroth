from __future__ import annotations

from datetime import datetime, timezone
from time import perf_counter
from typing import Any
from uuid import uuid4

from zeroth.econ.instrumentation.client import resolve_join_key
from zeroth.econ.instrumentation.integrations._capture import finalize_capture_metadata, should_capture_layer, should_emit_by_rate, start_time_ms
from zeroth.econ.instrumentation.runtime import get_runtime
from zeroth.econ.instrumentation.schemas import ExecutionEvent


def _new_run_id() -> str:
    return f"lg_{uuid4().hex}"


class _CapturedV3Driver:
    """Capture one econ event across the complete caller-driven v3 lifecycle."""

    def __init__(
        self,
        owner: InstrumentedLangGraph,
        run: Any,
        run_id: str,
        started: float,
        started_ms: int,
    ) -> None:
        self._owner = owner
        self._run = run
        self._pump = run._apump_next
        self._abort = run.abort
        self._mux_apush = run._mux.apush
        self._mux_aclose = run._mux.aclose
        self._run_id = run_id
        self._started = started
        self._started_ms = started_ms
        self._emitted = False
        self._terminal_error: BaseException | None = None

    def _remember_error(self, error: BaseException) -> None:
        if self._terminal_error is None:
            self._terminal_error = error

    def _error(self) -> BaseException | None:
        if self._terminal_error is not None:
            return self._terminal_error
        events = getattr(self._run._mux, "_events", None)
        return getattr(events, "_error", None)

    async def _finish(self, error: BaseException | None = None) -> None:
        if self._emitted:
            return
        self._emitted = True
        await self._owner._safe_aemit(
            self._run_id,
            self._started,
            self._started_ms,
            "astream_events",
            error=error,
            streaming=True,
        )

    async def apush(self, event: Any) -> None:
        try:
            await self._mux_apush(event)
        except BaseException as exc:
            self._remember_error(exc)
            raise

    async def aclose_mux(self) -> None:
        try:
            await self._mux_aclose()
        except BaseException as exc:
            self._remember_error(exc)
            raise

    async def pump(self) -> bool:
        with get_runtime().capture_context("langgraph", run_id=self._run_id):
            try:
                progressed = await self._pump()
            except BaseException as exc:
                self._remember_error(exc)
                await self._finish(exc)
                raise
            if not progressed:
                await self._finish(self._error())
            return progressed

    async def abort(self) -> None:
        with get_runtime().capture_context("langgraph", run_id=self._run_id):
            try:
                await self._abort()
            except BaseException as exc:
                self._remember_error(exc)
                await self._finish(exc)
                raise
            await self._finish(self._error())


class InstrumentedLangGraph:
    def __init__(self, graph: Any, capability_id: str, implementation_id: str, tags: dict[str, Any] | None = None):
        self._graph = graph
        self._capability_id = capability_id
        self._implementation_id = implementation_id
        self._tags = tags or {}

    def _event(self, run_id: str, elapsed_ms: int, metadata: dict[str, Any]) -> ExecutionEvent:
        join_key = resolve_join_key(run_id, {"run_id": run_id, **metadata})
        enriched = finalize_capture_metadata(
            metadata=metadata,
            source_layer="langgraph",
            provider=str(metadata.get("provider", "langgraph")),
            model=str(metadata.get("model", "langgraph")),
            join_key=join_key,
            run_id=run_id,
            start_ms=int(metadata.get("_start_ms", start_time_ms())),
        )
        return ExecutionEvent(
            execution_id=run_id,
            join_key=join_key,
            timestamp=datetime.now(timezone.utc),
            capability_id=self._capability_id,
            implementation_id=self._implementation_id,
            model_version=str(metadata.get("model", "langgraph")),
            latency_ms=elapsed_ms,
            compute_time_ms=elapsed_ms,
            metadata=enriched,
        )

    def _emit(self, run_id: str, started: float, started_ms: int, operation: str, error: BaseException | None = None, streaming: bool = False) -> None:
        elapsed_ms = int((perf_counter() - started) * 1000)
        metadata = {
            "provider": "langgraph",
            "library": "langgraph",
            "model": "langgraph",
            "operation": operation,
            "run_id": run_id,
            "tags": self._tags,
            "error": error is not None,
            "streaming": streaming,
            "deployment_mode": "unknown",
            "cost_inputs": {"provider": "langgraph", "model": "langgraph", "deployment_mode": "unknown"},
            "data_quality_hints": {"cost": "inferred", "value": "unknown"},
            "_start_ms": started_ms,
        }
        if error is not None:
            metadata["error_type"] = type(error).__name__
        event = self._event(run_id, elapsed_ms, metadata)
        if should_emit_by_rate("langgraph"):
            get_runtime().transport.enqueue_execution(event)

    def _safe_emit(self, run_id: str, started: float, started_ms: int, operation: str, error: BaseException | None = None, streaming: bool = False) -> None:
        # Best-effort telemetry: a failed emit must NEVER change the graph's return
        # value, mask its exception, or alter cancellation. Swallow Exception only
        # (never BaseException) so KeyboardInterrupt / CancelledError still propagate.
        try:
            self._emit(run_id, started, started_ms, operation, error=error, streaming=streaming)
        except Exception:
            pass

    async def _aemit(self, run_id: str, started: float, started_ms: int, operation: str, error: BaseException | None = None, streaming: bool = False) -> None:
        elapsed_ms = int((perf_counter() - started) * 1000)
        metadata = {
            "provider": "langgraph",
            "library": "langgraph",
            "model": "langgraph",
            "operation": operation,
            "run_id": run_id,
            "tags": self._tags,
            "error": error is not None,
            "streaming": streaming,
            "deployment_mode": "unknown",
            "cost_inputs": {"provider": "langgraph", "model": "langgraph", "deployment_mode": "unknown"},
            "data_quality_hints": {"cost": "inferred", "value": "unknown"},
            "_start_ms": started_ms,
        }
        if error is not None:
            metadata["error_type"] = type(error).__name__
        event = self._event(run_id, elapsed_ms, metadata)
        if should_emit_by_rate("langgraph"):
            await get_runtime().transport.aenqueue_execution(event)

    async def _safe_aemit(self, run_id: str, started: float, started_ms: int, operation: str, error: BaseException | None = None, streaming: bool = False) -> None:
        # Best-effort async telemetry: see _safe_emit. Exception is swallowed so a
        # transport failure cannot replace the result or unwind cancellation, while
        # CancelledError (BaseException) still propagates unchanged.
        try:
            await self._aemit(run_id, started, started_ms, operation, error=error, streaming=streaming)
        except Exception:
            pass

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        if not should_capture_layer("langgraph") or not get_runtime().config.enabled:
            return self._graph.invoke(*args, **kwargs)
        run_id = _new_run_id()
        started = perf_counter()
        started_ms = start_time_ms()
        error: BaseException | None = None
        with get_runtime().capture_context("langgraph", run_id=run_id):
            try:
                return self._graph.invoke(*args, **kwargs)
            except Exception as exc:
                error = exc
                raise
            finally:
                self._safe_emit(run_id, started, started_ms, "invoke", error=error)

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        if not should_capture_layer("langgraph") or not get_runtime().config.enabled:
            return await self._graph.ainvoke(*args, **kwargs)
        run_id = _new_run_id()
        started = perf_counter()
        started_ms = start_time_ms()
        error: BaseException | None = None
        with get_runtime().capture_context("langgraph", run_id=run_id):
            try:
                return await self._graph.ainvoke(*args, **kwargs)
            except Exception as exc:
                error = exc
                raise
            finally:
                await self._safe_aemit(run_id, started, started_ms, "ainvoke", error=error)

    def stream(self, *args: Any, **kwargs: Any):
        if not should_capture_layer("langgraph") or not get_runtime().config.enabled:
            yield from self._graph.stream(*args, **kwargs)
            return
        run_id = _new_run_id()
        started = perf_counter()
        started_ms = start_time_ms()
        error: BaseException | None = None
        with get_runtime().capture_context("langgraph", run_id=run_id):
            try:
                for chunk in self._graph.stream(*args, **kwargs):
                    yield chunk
            except Exception as exc:
                error = exc
                raise
            finally:
                self._safe_emit(run_id, started, started_ms, "stream", error=error, streaming=True)

    async def astream(self, *args: Any, **kwargs: Any):
        if not should_capture_layer("langgraph") or not get_runtime().config.enabled:
            async for chunk in self._graph.astream(*args, **kwargs):
                yield chunk
            return
        run_id = _new_run_id()
        started = perf_counter()
        started_ms = start_time_ms()
        error: BaseException | None = None
        with get_runtime().capture_context("langgraph", run_id=run_id):
            try:
                async for chunk in self._graph.astream(*args, **kwargs):
                    yield chunk
            except Exception as exc:
                error = exc
                raise
            finally:
                await self._safe_aemit(run_id, started, started_ms, "astream", error=error, streaming=True)

    def astream_events(self, *args: Any, **kwargs: Any) -> Any:
        if kwargs.get("version") == "v3":
            return self._astream_events_v3(*args, **kwargs)
        return self._astream_events_v1_v2(*args, **kwargs)

    async def _astream_events_v1_v2(self, *args: Any, **kwargs: Any):
        iterator = self._graph.astream_events(*args, **kwargs).__aiter__()
        if not should_capture_layer("langgraph") or not get_runtime().config.enabled:
            try:
                while True:
                    yield await iterator.__anext__()
            except StopAsyncIteration:
                return
            finally:
                aclose = getattr(iterator, "aclose", None)
                if aclose is not None:
                    await aclose()
        run_id = _new_run_id()
        started = perf_counter()
        started_ms = start_time_ms()
        error: BaseException | None = None
        with get_runtime().capture_context("langgraph", run_id=run_id):
            try:
                while True:
                    yield await iterator.__anext__()
            except StopAsyncIteration:
                return
            except Exception as exc:
                error = exc
                raise
            finally:
                aclose = getattr(iterator, "aclose", None)
                if aclose is not None:
                    await aclose()
                await self._safe_aemit(
                    run_id,
                    started,
                    started_ms,
                    "astream_events",
                    error=error,
                    streaming=True,
                )

    async def _astream_events_v3(self, *args: Any, **kwargs: Any) -> Any:
        if not should_capture_layer("langgraph") or not get_runtime().config.enabled:
            return await self._graph.astream_events(*args, **kwargs)

        run_id = _new_run_id()
        started = perf_counter()
        started_ms = start_time_ms()
        with get_runtime().capture_context("langgraph", run_id=run_id):
            try:
                run = await self._graph.astream_events(*args, **kwargs)
            except BaseException as exc:
                await self._safe_aemit(
                    run_id,
                    started,
                    started_ms,
                    "astream_events",
                    error=exc,
                    streaming=True,
                )
                raise
        capture = _CapturedV3Driver(
            self,
            run,
            run_id,
            started,
            started_ms,
        )
        run._mux.apush = capture.apush
        run._mux.aclose = capture.aclose_mux
        run._apump_next = capture.pump
        run.abort = capture.abort
        run._mux.bind_apump(capture.pump)
        return run

    def __getattr__(self, item: str) -> Any:
        return getattr(self._graph, item)


def instrument_langgraph_graph(graph: Any, capability_id: str, implementation_id: str, tags: dict[str, Any] | None = None) -> Any:
    return InstrumentedLangGraph(graph, capability_id, implementation_id, tags)
