from __future__ import annotations

import inspect
import os
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Awaitable, Callable

from zeroth.core.econ.instrumentation.integrations._capture import (
    finalize_capture_metadata,
    should_capture_provider,
    should_emit_by_rate,
    start_time_ms,
)
from zeroth.core.econ.instrumentation.otel import maybe_span
from zeroth.core.econ.instrumentation.runtime import get_runtime
from zeroth.core.econ.instrumentation.schemas import ExecutionEvent
from zeroth.core.econ.instrumentation.client import resolve_join_key


def _usage_tokens(response: Any) -> tuple[int, int, int]:
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return 0, 0, 0

    prompt = getattr(usage, "prompt_tokens", None)
    completion = getattr(usage, "completion_tokens", None)
    total = getattr(usage, "total_tokens", None)
    if isinstance(usage, dict):
        prompt = usage.get("prompt_tokens", prompt)
        completion = usage.get("completion_tokens", completion)
        total = usage.get("total_tokens", total)

    p = int(prompt or 0)
    c = int(completion or 0)
    t = int(total or (p + c))
    return p, c, t


def _model_name(response: Any, fallback: str) -> str:
    model = getattr(response, "model", None)
    if model is None and isinstance(response, dict):
        model = response.get("model")
    return str(model or fallback)


def _enabled() -> bool:
    return os.getenv("ECP_INSTRUMENT_OPENAI", "false").lower() == "true"


def _build_event(
    capability_id: str,
    implementation_id: str,
    elapsed_ms: int,
    model_name: str,
    metadata: dict[str, Any],
) -> ExecutionEvent:
    execution_id = f"oa_{int(datetime.now(timezone.utc).timestamp() * 1000)}"
    run_id = str(metadata.get("run_id") or execution_id)
    join_key = resolve_join_key(execution_id, metadata)
    enriched = finalize_capture_metadata(
        metadata=metadata,
        source_layer="openai",
        provider="openai",
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
        if not should_capture_provider("openai"):
            return fn(*args, **kwargs)

        start = perf_counter()
        started_ms = start_time_ms()
        model_hint = str(kwargs.get("model", "unknown"))
        run_id = str(kwargs.get("run_id") or runtime.active_run_id() or f"oa_run_{started_ms}")
        metadata: dict[str, Any] = {
            "provider": "openai",
            "library": "openai",
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
            "ecp.openai.call",
            {
                "ecp.capability_id": capability_id,
                "ecp.implementation_id": implementation_id,
                "llm.provider": "openai",
                "llm.model": model_hint,
            },
        ):
            try:
                response = fn(*args, **kwargs)
                prompt, completion, total = _usage_tokens(response)
                provider_request_id = getattr(response, "id", None)
                if provider_request_id is None and isinstance(response, dict):
                    provider_request_id = response.get("id")
                metadata.update(
                    {
                        "prompt_tokens": prompt,
                        "completion_tokens": completion,
                        "total_tokens": total,
                        "usage_missing": total == 0,
                        "cost_inputs": {
                            "prompt_tokens": prompt,
                            "completion_tokens": completion,
                            "provider": "openai",
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
                if should_emit_by_rate("openai"):
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
        if not should_capture_provider("openai"):
            return await fn(*args, **kwargs)

        start = perf_counter()
        started_ms = start_time_ms()
        model_hint = str(kwargs.get("model", "unknown"))
        run_id = str(kwargs.get("run_id") or runtime.active_run_id() or f"oa_run_{started_ms}")
        metadata: dict[str, Any] = {
            "provider": "openai",
            "library": "openai",
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
            "ecp.openai.call",
            {
                "ecp.capability_id": capability_id,
                "ecp.implementation_id": implementation_id,
                "llm.provider": "openai",
                "llm.model": model_hint,
            },
        ):
            try:
                response = await fn(*args, **kwargs)
                prompt, completion, total = _usage_tokens(response)
                provider_request_id = getattr(response, "id", None)
                if provider_request_id is None and isinstance(response, dict):
                    provider_request_id = response.get("id")
                metadata.update(
                    {
                        "prompt_tokens": prompt,
                        "completion_tokens": completion,
                        "total_tokens": total,
                        "usage_missing": total == 0,
                        "cost_inputs": {
                            "prompt_tokens": prompt,
                            "completion_tokens": completion,
                            "provider": "openai",
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
                if should_emit_by_rate("openai"):
                    await runtime.transport.aenqueue_execution(event)

    return wrapped


def _patch_openai_client(client: Any, capability_id: str, implementation_id: str, tags: dict[str, Any]) -> Any:
    target_paths = [
        ("chat", "completions", "create"),
        ("responses", "create"),
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


def instrument_openai_client(client: Any, capability_id: str, implementation_id: str, tags: dict[str, Any] | None = None) -> Any:
    return _patch_openai_client(client, capability_id, implementation_id, tags or {})


def instrument_openai_async_client(client: Any, capability_id: str, implementation_id: str, tags: dict[str, Any] | None = None) -> Any:
    return _patch_openai_client(client, capability_id, implementation_id, tags or {})
