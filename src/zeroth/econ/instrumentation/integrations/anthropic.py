from __future__ import annotations

import inspect
import os
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Awaitable, Callable
from uuid import uuid4

from zeroth.econ.instrumentation.integrations._capture import (
    finalize_capture_metadata,
    should_capture_provider,
    should_emit_by_rate,
    start_time_ms,
)
from zeroth.econ.instrumentation.otel import maybe_span
from zeroth.econ.instrumentation.runtime import get_runtime
from zeroth.econ.instrumentation.schemas import ExecutionEvent
from zeroth.econ.measurement import MeasurementState
from zeroth.econ.instrumentation.client import resolve_join_key


def _usage_tokens(response: Any) -> tuple[int | None, int | None, int | None]:
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return None, None, None

    inp = getattr(usage, "input_tokens", None)
    out = getattr(usage, "output_tokens", None)
    if isinstance(usage, dict):
        inp = usage.get("input_tokens", inp)
        out = usage.get("output_tokens", out)

    i = int(inp) if inp is not None else None
    o = int(out) if out is not None else None
    return i, o, i + o if i is not None and o is not None else None


def _enabled() -> bool:
    return os.getenv("ECP_INSTRUMENT_ANTHROPIC", "false").lower() == "true"


def _model_name(response: Any, fallback: str) -> str:
    model = getattr(response, "model", None)
    if model is None and isinstance(response, dict):
        model = response.get("model")
    return str(model or fallback)


def _build_event(
    capability_id: str,
    implementation_id: str,
    elapsed_ms: int,
    model_name: str,
    metadata: dict[str, Any],
) -> ExecutionEvent:
    execution_id = f"an_{uuid4().hex}"
    run_id = str(metadata.get("run_id") or execution_id)
    join_key = resolve_join_key(execution_id, metadata)
    enriched = finalize_capture_metadata(
        metadata=metadata,
        source_layer="anthropic",
        provider="anthropic",
        model=model_name,
        join_key=join_key,
        run_id=run_id,
        start_ms=int(metadata.get("_start_ms", start_time_ms())),
    )
    return ExecutionEvent(
        execution_id=execution_id,
        join_key=join_key,
        timestamp=datetime.now(timezone.utc),
        capability_id=capability_id,
        implementation_id=implementation_id,
        model_version=model_name,
        latency_ms=elapsed_ms,
        compute_time_ms=elapsed_ms,
        usage_measurement=(
            MeasurementState.MEASURED
            if metadata.get("total_tokens") is not None
            else MeasurementState.UNMEASURED
        ),
        metadata=enriched,
    )


def _wrap_sync_call(
    fn: Callable[..., Any],
    capability_id: str,
    implementation_id: str,
    tags: dict[str, Any],
) -> Callable[..., Any]:
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        runtime = get_runtime()
        if not _enabled() or not runtime.config.enabled:
            return fn(*args, **kwargs)
        if not should_capture_provider("anthropic"):
            return fn(*args, **kwargs)

        start = perf_counter()
        started_ms = start_time_ms()
        model_hint = str(kwargs.get("model", "unknown"))
        run_id = str(kwargs.get("run_id") or runtime.active_run_id() or f"an_run_{started_ms}")
        metadata: dict[str, Any] = {
            "provider": "anthropic",
            "library": "anthropic",
            "model": model_hint,
            "is_async": False,
            "run_id": run_id,
            "tags": tags,
            "usage_missing": False,
            "error": False,
            "deployment_mode": "hosted",
            "data_quality_hints": {"cost": "mixed", "value": "unknown"},
            "_start_ms": started_ms,
        }
        with maybe_span(
            get_runtime().config.enable_otel,
            "ecp.anthropic.call",
            {
                "ecp.capability_id": capability_id,
                "ecp.implementation_id": implementation_id,
                "llm.provider": "anthropic",
                "llm.model": model_hint,
            },
        ):
            try:
                response = fn(*args, **kwargs)
                inp, out, total = _usage_tokens(response)
                provider_request_id = getattr(response, "id", None)
                if provider_request_id is None and isinstance(response, dict):
                    provider_request_id = response.get("id")
                metadata.update(
                    {
                        "input_tokens": inp,
                        "output_tokens": out,
                        "total_tokens": total,
                        "usage_missing": total is None,
                        "cost_inputs": {
                            "prompt_tokens": inp,
                            "completion_tokens": out,
                            "provider": "anthropic",
                            "model": model_hint,
                            "deployment_mode": "hosted",
                        },
                        "provider_request_id": str(provider_request_id) if provider_request_id else None,
                    }
                )
                model_name = _model_name(response, model_hint)
                return response
            except Exception as exc:
                metadata.update({"error": True, "error_type": type(exc).__name__})
                model_name = model_hint
                raise
            finally:
                elapsed_ms = int((perf_counter() - start) * 1000)
                event = _build_event(capability_id, implementation_id, elapsed_ms, model_name, metadata)
                if should_emit_by_rate("anthropic"):
                    runtime.transport.enqueue_execution(event)

    return wrapped


def _wrap_async_call(
    fn: Callable[..., Awaitable[Any]],
    capability_id: str,
    implementation_id: str,
    tags: dict[str, Any],
) -> Callable[..., Awaitable[Any]]:
    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        runtime = get_runtime()
        if not _enabled() or not runtime.config.enabled:
            return await fn(*args, **kwargs)
        if not should_capture_provider("anthropic"):
            return await fn(*args, **kwargs)

        start = perf_counter()
        started_ms = start_time_ms()
        model_hint = str(kwargs.get("model", "unknown"))
        run_id = str(kwargs.get("run_id") or runtime.active_run_id() or f"an_run_{started_ms}")
        metadata: dict[str, Any] = {
            "provider": "anthropic",
            "library": "anthropic",
            "model": model_hint,
            "is_async": True,
            "run_id": run_id,
            "tags": tags,
            "usage_missing": False,
            "error": False,
            "deployment_mode": "hosted",
            "data_quality_hints": {"cost": "mixed", "value": "unknown"},
            "_start_ms": started_ms,
        }
        with maybe_span(
            get_runtime().config.enable_otel,
            "ecp.anthropic.call",
            {
                "ecp.capability_id": capability_id,
                "ecp.implementation_id": implementation_id,
                "llm.provider": "anthropic",
                "llm.model": model_hint,
            },
        ):
            try:
                response = await fn(*args, **kwargs)
                inp, out, total = _usage_tokens(response)
                provider_request_id = getattr(response, "id", None)
                if provider_request_id is None and isinstance(response, dict):
                    provider_request_id = response.get("id")
                metadata.update(
                    {
                        "input_tokens": inp,
                        "output_tokens": out,
                        "total_tokens": total,
                        "usage_missing": total is None,
                        "cost_inputs": {
                            "prompt_tokens": inp,
                            "completion_tokens": out,
                            "provider": "anthropic",
                            "model": model_hint,
                            "deployment_mode": "hosted",
                        },
                        "provider_request_id": str(provider_request_id) if provider_request_id else None,
                    }
                )
                model_name = _model_name(response, model_hint)
                return response
            except Exception as exc:
                metadata.update({"error": True, "error_type": type(exc).__name__})
                model_name = model_hint
                raise
            finally:
                elapsed_ms = int((perf_counter() - start) * 1000)
                event = _build_event(capability_id, implementation_id, elapsed_ms, model_name, metadata)
                if should_emit_by_rate("anthropic"):
                    await runtime.transport.aenqueue_execution(event)

    return wrapped


def _patch_anthropic_client(client: Any, capability_id: str, implementation_id: str, tags: dict[str, Any]) -> Any:
    target_paths = [
        ("messages", "create"),
    ]

    for path in target_paths:
        obj = client
        try:
            for step in path[:-1]:
                obj = getattr(obj, step)
            method_name = path[-1]
            original = getattr(obj, method_name)
            wrapped = (
                _wrap_async_call(original, capability_id, implementation_id, tags)
                if inspect.iscoroutinefunction(original)
                else _wrap_sync_call(original, capability_id, implementation_id, tags)
            )
            setattr(obj, method_name, wrapped)
        except Exception:
            continue
    return client


def instrument_anthropic_client(client: Any, capability_id: str, implementation_id: str, tags: dict[str, Any] | None = None) -> Any:
    return _patch_anthropic_client(client, capability_id, implementation_id, tags or {})


def instrument_anthropic_async_client(client: Any, capability_id: str, implementation_id: str, tags: dict[str, Any] | None = None) -> Any:
    return _patch_anthropic_client(client, capability_id, implementation_id, tags or {})
