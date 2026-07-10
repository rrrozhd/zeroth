from __future__ import annotations

import os
from datetime import datetime, timezone
from time import perf_counter
from typing import Any

from zeroth.core.econ.instrumentation.client import resolve_join_key
from zeroth.core.econ.instrumentation.integrations._capture import finalize_capture_metadata, should_capture_layer, should_emit_by_rate, start_time_ms
from zeroth.core.econ.instrumentation.runtime import get_runtime
from zeroth.core.econ.instrumentation.schemas import ExecutionEvent


def _enabled() -> bool:
    return os.getenv("ECP_INSTRUMENT_LANGCHAIN", "false").lower() == "true" and should_capture_layer("langchain")


def _execution_event(
    *,
    capability_id: str,
    implementation_id: str,
    run_id: str,
    elapsed_ms: int,
    metadata: dict[str, Any],
) -> ExecutionEvent:
    join_key = resolve_join_key(run_id, {"run_id": run_id, **metadata})
    enriched = finalize_capture_metadata(
        metadata=metadata,
        source_layer="langchain",
        provider=str(metadata.get("provider", "unknown")),
        model=str(metadata.get("model", "langchain")),
        join_key=join_key,
        run_id=run_id,
        start_ms=int(metadata.get("_start_ms", start_time_ms())),
    )
    return ExecutionEvent(
        execution_id=run_id,
        join_key=join_key,
        timestamp=datetime.now(timezone.utc),
        capability_id=capability_id,
        implementation_id=implementation_id,
        model_version=str(metadata.get("model", "langchain")),
        latency_ms=elapsed_ms,
        compute_time_ms=elapsed_ms,
        metadata=enriched,
    )


class EconLangChainCallbackHandler:
    def __init__(self, capability_id: str, implementation_id: str, tags: dict[str, Any] | None = None):
        self.capability_id = capability_id
        self.implementation_id = implementation_id
        self.tags = tags or {}
        self._run_starts: dict[str, float] = {}
        self._run_starts_ms: dict[str, int] = {}

    def _start(self, run_id: str) -> None:
        self._run_starts[run_id] = perf_counter()
        self._run_starts_ms[run_id] = start_time_ms()

    def _emit(self, run_id: str, kind: str, error: BaseException | None = None, extra: dict[str, Any] | None = None) -> None:
        if not _enabled() or not get_runtime().config.enabled:
            return
        started = self._run_starts.pop(run_id, perf_counter())
        start_ms = self._run_starts_ms.pop(run_id, start_time_ms())
        elapsed_ms = int((perf_counter() - started) * 1000)
        metadata = {
            "provider": "unknown",
            "library": "langchain",
            "kind": kind,
            "run_id": run_id,
            "tags": self.tags,
            "error": error is not None,
            "deployment_mode": "unknown",
            "model": "langchain",
            "cost_inputs": {"provider": "unknown", "model": "langchain", "deployment_mode": "unknown"},
            "data_quality_hints": {"cost": "inferred", "value": "unknown"},
            "_start_ms": start_ms,
        }
        if extra:
            metadata.update(extra)
        if error is not None:
            metadata["error_type"] = type(error).__name__
        event = _execution_event(
            capability_id=self.capability_id,
            implementation_id=self.implementation_id,
            run_id=run_id,
            elapsed_ms=elapsed_ms,
            metadata=metadata,
        )
        if should_emit_by_rate("langchain"):
            get_runtime().transport.enqueue_execution(event)

    def on_chain_start(self, serialized: dict[str, Any], inputs: dict[str, Any], run_id: str, **kwargs: Any) -> None:
        del serialized, inputs, kwargs
        self._start(str(run_id))

    def on_chain_end(self, outputs: dict[str, Any], run_id: str, **kwargs: Any) -> None:
        del outputs, kwargs
        self._emit(str(run_id), "chain")

    def on_chain_error(self, error: BaseException, run_id: str, **kwargs: Any) -> None:
        del kwargs
        self._emit(str(run_id), "chain", error=error)

    def on_llm_start(self, serialized: dict[str, Any], prompts: list[str], run_id: str, **kwargs: Any) -> None:
        del serialized, prompts, kwargs
        self._start(str(run_id))

    def on_llm_end(self, response: Any, run_id: str, **kwargs: Any) -> None:
        del response, kwargs
        self._emit(str(run_id), "llm")

    def on_llm_error(self, error: BaseException, run_id: str, **kwargs: Any) -> None:
        del kwargs
        self._emit(str(run_id), "llm", error=error)

    def on_tool_start(self, serialized: dict[str, Any], input_str: str, run_id: str, **kwargs: Any) -> None:
        del serialized, input_str, kwargs
        self._start(str(run_id))

    def on_tool_end(self, output: Any, run_id: str, **kwargs: Any) -> None:
        del output, kwargs
        self._emit(str(run_id), "tool")

    def on_tool_error(self, error: BaseException, run_id: str, **kwargs: Any) -> None:
        del kwargs
        self._emit(str(run_id), "tool", error=error)

    def on_retriever_start(self, serialized: dict[str, Any], query: str, run_id: str, **kwargs: Any) -> None:
        del serialized, query, kwargs
        self._start(str(run_id))

    def on_retriever_end(self, documents: Any, run_id: str, **kwargs: Any) -> None:
        del documents, kwargs
        self._emit(str(run_id), "retriever")

    def on_retriever_error(self, error: BaseException, run_id: str, **kwargs: Any) -> None:
        del kwargs
        self._emit(str(run_id), "retriever", error=error)


class InstrumentedRunnable:
    def __init__(self, runnable: Any, capability_id: str, implementation_id: str, tags: dict[str, Any] | None = None):
        self._runnable = runnable
        self._capability_id = capability_id
        self._implementation_id = implementation_id
        self._tags = tags or {}

    def _emit(self, run_id: str, started: float, started_ms: int, op: str, error: BaseException | None = None, streaming: bool = False) -> None:
        elapsed_ms = int((perf_counter() - started) * 1000)
        metadata = {
            "provider": "unknown",
            "library": "langchain",
            "model": "langchain",
            "operation": op,
            "run_id": run_id,
            "tags": self._tags,
            "error": error is not None,
            "streaming": streaming,
            "deployment_mode": "unknown",
            "cost_inputs": {"provider": "unknown", "model": "langchain", "deployment_mode": "unknown"},
            "data_quality_hints": {"cost": "inferred", "value": "unknown"},
            "_start_ms": started_ms,
        }
        if error is not None:
            metadata["error_type"] = type(error).__name__
        event = _execution_event(
            capability_id=self._capability_id,
            implementation_id=self._implementation_id,
            run_id=run_id,
            elapsed_ms=elapsed_ms,
            metadata=metadata,
        )
        if should_emit_by_rate("langchain"):
            get_runtime().transport.enqueue_execution(event)

    async def _aemit(self, run_id: str, started: float, started_ms: int, op: str, error: BaseException | None = None, streaming: bool = False) -> None:
        elapsed_ms = int((perf_counter() - started) * 1000)
        metadata = {
            "provider": "unknown",
            "library": "langchain",
            "model": "langchain",
            "operation": op,
            "run_id": run_id,
            "tags": self._tags,
            "error": error is not None,
            "streaming": streaming,
            "deployment_mode": "unknown",
            "cost_inputs": {"provider": "unknown", "model": "langchain", "deployment_mode": "unknown"},
            "data_quality_hints": {"cost": "inferred", "value": "unknown"},
            "_start_ms": started_ms,
        }
        if error is not None:
            metadata["error_type"] = type(error).__name__
        event = _execution_event(
            capability_id=self._capability_id,
            implementation_id=self._implementation_id,
            run_id=run_id,
            elapsed_ms=elapsed_ms,
            metadata=metadata,
        )
        if should_emit_by_rate("langchain"):
            await get_runtime().transport.aenqueue_execution(event)

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        if not _enabled() or not get_runtime().config.enabled:
            return self._runnable.invoke(*args, **kwargs)
        run_id = f"lc_{int(datetime.now(timezone.utc).timestamp() * 1000)}"
        started = perf_counter()
        started_ms = start_time_ms()
        error: BaseException | None = None
        with get_runtime().capture_context("langchain", run_id=run_id):
            try:
                return self._runnable.invoke(*args, **kwargs)
            except Exception as exc:
                error = exc
                raise
            finally:
                self._emit(run_id, started, started_ms, "invoke", error=error)

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        if not _enabled() or not get_runtime().config.enabled:
            return await self._runnable.ainvoke(*args, **kwargs)
        run_id = f"lc_{int(datetime.now(timezone.utc).timestamp() * 1000)}"
        started = perf_counter()
        started_ms = start_time_ms()
        error: BaseException | None = None
        with get_runtime().capture_context("langchain", run_id=run_id):
            try:
                return await self._runnable.ainvoke(*args, **kwargs)
            except Exception as exc:
                error = exc
                raise
            finally:
                await self._aemit(run_id, started, started_ms, "ainvoke", error=error)

    def batch(self, *args: Any, **kwargs: Any) -> Any:
        if not _enabled() or not get_runtime().config.enabled or not hasattr(self._runnable, "batch"):
            return self._runnable.batch(*args, **kwargs)
        run_id = f"lc_{int(datetime.now(timezone.utc).timestamp() * 1000)}"
        started = perf_counter()
        started_ms = start_time_ms()
        error: BaseException | None = None
        with get_runtime().capture_context("langchain", run_id=run_id):
            try:
                return self._runnable.batch(*args, **kwargs)
            except Exception as exc:
                error = exc
                raise
            finally:
                self._emit(run_id, started, started_ms, "batch", error=error)

    async def abatch(self, *args: Any, **kwargs: Any) -> Any:
        if not _enabled() or not get_runtime().config.enabled or not hasattr(self._runnable, "abatch"):
            return await self._runnable.abatch(*args, **kwargs)
        run_id = f"lc_{int(datetime.now(timezone.utc).timestamp() * 1000)}"
        started = perf_counter()
        started_ms = start_time_ms()
        error: BaseException | None = None
        with get_runtime().capture_context("langchain", run_id=run_id):
            try:
                return await self._runnable.abatch(*args, **kwargs)
            except Exception as exc:
                error = exc
                raise
            finally:
                await self._aemit(run_id, started, started_ms, "abatch", error=error)

    def stream(self, *args: Any, **kwargs: Any):
        if not _enabled() or not get_runtime().config.enabled or not hasattr(self._runnable, "stream"):
            yield from self._runnable.stream(*args, **kwargs)
            return
        run_id = f"lc_{int(datetime.now(timezone.utc).timestamp() * 1000)}"
        started = perf_counter()
        started_ms = start_time_ms()
        error: BaseException | None = None
        with get_runtime().capture_context("langchain", run_id=run_id):
            try:
                for chunk in self._runnable.stream(*args, **kwargs):
                    yield chunk
            except Exception as exc:
                error = exc
                raise
            finally:
                self._emit(run_id, started, started_ms, "stream", error=error, streaming=True)

    async def astream(self, *args: Any, **kwargs: Any):
        if not _enabled() or not get_runtime().config.enabled or not hasattr(self._runnable, "astream"):
            async for chunk in self._runnable.astream(*args, **kwargs):
                yield chunk
            return
        run_id = f"lc_{int(datetime.now(timezone.utc).timestamp() * 1000)}"
        started = perf_counter()
        started_ms = start_time_ms()
        error: BaseException | None = None
        with get_runtime().capture_context("langchain", run_id=run_id):
            try:
                async for chunk in self._runnable.astream(*args, **kwargs):
                    yield chunk
            except Exception as exc:
                error = exc
                raise
            finally:
                await self._aemit(run_id, started, started_ms, "astream", error=error, streaming=True)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._runnable, item)


def instrument_langchain_callback_handler(
    capability_id: str,
    implementation_id: str,
    tags: dict[str, Any] | None = None,
) -> EconLangChainCallbackHandler:
    return EconLangChainCallbackHandler(capability_id, implementation_id, tags)


def instrument_langchain_runnable(runnable: Any, capability_id: str, implementation_id: str, tags: dict[str, Any] | None = None) -> Any:
    return InstrumentedRunnable(runnable, capability_id, implementation_id, tags)


def instrument_langchain_async_runnable(runnable: Any, capability_id: str, implementation_id: str, tags: dict[str, Any] | None = None) -> Any:
    return InstrumentedRunnable(runnable, capability_id, implementation_id, tags)


def instrument_langchain_app(app: Any, capability_id: str, implementation_id: str, tags: dict[str, Any] | None = None) -> Any:
    handler = instrument_langchain_callback_handler(capability_id, implementation_id, tags)
    if hasattr(app, "with_config"):
        try:
            return app.with_config({"callbacks": [handler]})
        except Exception:
            pass
    return instrument_langchain_runnable(app, capability_id, implementation_id, tags)
