from __future__ import annotations

from datetime import datetime, timezone
from time import perf_counter
from typing import Any

from econ_instrumentation.client import resolve_join_key
from econ_instrumentation.integrations._capture import finalize_capture_metadata, should_capture_layer, should_emit_by_rate, start_time_ms
from econ_instrumentation.runtime import get_runtime
from econ_instrumentation.schemas import ExecutionEvent


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

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        if not should_capture_layer("langgraph") or not get_runtime().config.enabled:
            return self._graph.invoke(*args, **kwargs)
        run_id = f"lg_{int(datetime.now(timezone.utc).timestamp() * 1000)}"
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
                self._emit(run_id, started, started_ms, "invoke", error=error)

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        if not should_capture_layer("langgraph") or not get_runtime().config.enabled:
            return await self._graph.ainvoke(*args, **kwargs)
        run_id = f"lg_{int(datetime.now(timezone.utc).timestamp() * 1000)}"
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
                await self._aemit(run_id, started, started_ms, "ainvoke", error=error)

    def stream(self, *args: Any, **kwargs: Any):
        if not should_capture_layer("langgraph") or not get_runtime().config.enabled:
            yield from self._graph.stream(*args, **kwargs)
            return
        run_id = f"lg_{int(datetime.now(timezone.utc).timestamp() * 1000)}"
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
                self._emit(run_id, started, started_ms, "stream", error=error, streaming=True)

    async def astream(self, *args: Any, **kwargs: Any):
        if not should_capture_layer("langgraph") or not get_runtime().config.enabled:
            async for chunk in self._graph.astream(*args, **kwargs):
                yield chunk
            return
        run_id = f"lg_{int(datetime.now(timezone.utc).timestamp() * 1000)}"
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
                await self._aemit(run_id, started, started_ms, "astream", error=error, streaming=True)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._graph, item)


def instrument_langgraph_graph(graph: Any, capability_id: str, implementation_id: str, tags: dict[str, Any] | None = None) -> Any:
    return InstrumentedLangGraph(graph, capability_id, implementation_id, tags)
