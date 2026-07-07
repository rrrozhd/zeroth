from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Optional
from uuid import NAMESPACE_URL, uuid5
import threading
import warnings

from econ_instrumentation.config import AutoInstrumentationConfig, InstrumentationConfig
from econ_instrumentation.runtime import get_runtime
from econ_instrumentation.schemas import ExecutionEvent, OutcomeEvent
from econ_instrumentation.transport import TelemetryTransport


class InstrumentationClient:
    def __init__(
        self,
        base_url: str = "http://localhost:8000/v1",
        timeout: float = 5.0,
        enabled: bool = True,
        buffer_max_events: int = 1000,
        flush_interval_ms: int = 500,
        headers_provider: Optional[Callable[[], dict[str, Any]]] = None,
    ):
        self.config = InstrumentationConfig(
            base_url=base_url.rstrip("/"),
            request_timeout_s=timeout,
            enabled=enabled,
            buffer_max_events=buffer_max_events,
            flush_interval_ms=flush_interval_ms,
        )
        # headers_provider (Zeroth vendor addition): forwarded to the transport so
        # each flush can attach fresh auth headers. See VENDOR.md.
        self.transport = TelemetryTransport(self.config, headers_provider=headers_provider)
        self.transport.start()

    def track_execution(self, event: ExecutionEvent) -> None:
        self.transport.enqueue_execution(event)

    def track_outcome(self, outcome: OutcomeEvent) -> None:
        self.transport.enqueue_outcome(outcome)


_default_client = InstrumentationClient()
_current_join_key: ContextVar[str | None] = ContextVar("ecp_join_key", default=None)
_execution_join_key: dict[str, str] = {}
_execution_join_key_lock = threading.Lock()


def resolve_join_key(
    execution_id: str,
    metadata: dict[str, Any] | None = None,
    explicit_request_id: str | None = None,
) -> str:
    metadata = metadata or {}
    if explicit_request_id:
        return explicit_request_id
    context_join_key = _current_join_key.get()
    if context_join_key:
        return context_join_key
    for candidate in ("request_id", "trace_id", "run_id"):
        value = metadata.get(candidate)
        if value:
            return str(value)
    material = f"{execution_id}:{metadata.get('capability_id', '')}:{metadata.get('implementation_id', '')}"
    return str(uuid5(NAMESPACE_URL, material))


def track_execution(event: ExecutionEvent) -> None:
    if not event.join_key:
        event.join_key = resolve_join_key(event.execution_id, event.metadata)
    runtime = get_runtime()
    if runtime.auto_enabled and runtime.auto_config.strict_join_key and not event.join_key:
        raise ValueError("join_key is required for execution telemetry in strict mode")
    with _execution_join_key_lock:
        _execution_join_key[event.execution_id] = event.join_key
    runtime.transport.enqueue_execution(event)


def track_outcome(outcome: OutcomeEvent) -> None:
    if not outcome.join_key:
        with _execution_join_key_lock:
            outcome.join_key = _execution_join_key.get(outcome.execution_id)
        if not outcome.join_key:
            context_join_key = _current_join_key.get()
            outcome.join_key = context_join_key or outcome.execution_id
    runtime = get_runtime()
    if runtime.auto_enabled and runtime.auto_config.strict_join_key and not outcome.join_key:
        raise ValueError("join_key is required for outcome telemetry in strict mode")
    if runtime.auto_enabled and runtime.auto_config.strict_join_key and outcome.join_key == outcome.execution_id:
        warnings.warn(
            "Outcome join_key fell back to execution_id; provide business request_id/case_id for strict correlation.",
            stacklevel=2,
        )
    runtime.transport.enqueue_outcome(outcome)


def configure(config: InstrumentationConfig) -> None:
    get_runtime().configure(config)
    with _execution_join_key_lock:
        _execution_join_key.clear()


def enable_auto_instrumentation(config: AutoInstrumentationConfig) -> None:
    get_runtime().enable_auto_instrumentation(config)


def disable_auto_instrumentation() -> None:
    get_runtime().disable_auto_instrumentation()


def build_cost_profile_input(
    requests_per_period: int,
    avg_input_tokens: float,
    avg_output_tokens: float,
    model_mix_json: dict[str, float],
    provider_rate_overrides_json: dict[str, dict[str, float]],
    avg_tool_calls_per_request_json: dict[str, float],
    tool_unit_price_overrides_json: dict[str, float],
    infra_monthly_spend_usd: float,
    retry_rate: float,
    cache_hit_rate: float,
) -> dict[str, object]:
    return {
        "requests_per_period": requests_per_period,
        "avg_input_tokens": avg_input_tokens,
        "avg_output_tokens": avg_output_tokens,
        "model_mix_json": model_mix_json,
        "provider_rate_overrides_json": provider_rate_overrides_json,
        "avg_tool_calls_per_request_json": avg_tool_calls_per_request_json,
        "tool_unit_price_overrides_json": tool_unit_price_overrides_json,
        "infra_monthly_spend_usd": infra_monthly_spend_usd,
        "retry_rate": retry_rate,
        "cache_hit_rate": cache_hit_rate,
    }


@contextmanager
def with_instrumentation(capability_id: str, implementation_id: str, tags: Optional[dict[str, Any]] = None) -> Iterator[dict[str, Any]]:
    started_at = perf_counter()
    exec_id = f"ctx_{int(datetime.now(timezone.utc).timestamp() * 1000)}"
    resolved_join_key = resolve_join_key(exec_id, {"capability_id": capability_id, "implementation_id": implementation_id, **(tags or {})})
    state = {"execution_id": exec_id, "join_key": resolved_join_key, "capability_id": capability_id, "implementation_id": implementation_id}
    token = _current_join_key.set(resolved_join_key)
    try:
        yield state
    finally:
        _current_join_key.reset(token)
        elapsed_ms = int((perf_counter() - started_at) * 1000)
        event = ExecutionEvent(
            execution_id=exec_id,
            join_key=resolved_join_key,
            capability_id=capability_id,
            implementation_id=implementation_id,
            latency_ms=elapsed_ms,
            compute_time_ms=elapsed_ms,
            metadata=tags or {},
        )
        track_execution(event)


@contextmanager
def join_key_context(join_key: str) -> Iterator[None]:
    token = _current_join_key.set(join_key)
    try:
        yield
    finally:
        _current_join_key.reset(token)
