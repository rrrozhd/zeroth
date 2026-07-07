from __future__ import annotations

import hashlib
import threading
import time
import os
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from econ_instrumentation.config import AutoInstrumentationConfig, InstrumentationConfig
from econ_instrumentation.transport import TelemetryTransport


class RuntimeState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.config = InstrumentationConfig.from_env()
        self.auto_enabled = os.getenv("ECP_AUTO_INSTRUMENT", "false").lower() == "true"
        self.auto_config = AutoInstrumentationConfig(
            capture_mode=os.getenv("ECP_CAPTURE_MODE", "hierarchical"),
        )
        self.transport = TelemetryTransport(self.config)
        self.transport.start()
        self._active_layers: ContextVar[tuple[str, ...]] = ContextVar("ecp_active_layers", default=())
        self._run_stack: ContextVar[tuple[str, ...]] = ContextVar("ecp_run_stack", default=())
        self._dedupe_lock = threading.Lock()
        self._dedupe_primary: dict[str, tuple[str, float]] = {}
        self._last_emit_ts: dict[str, float] = {}

    def configure(self, config: InstrumentationConfig) -> None:
        with self._lock:
            stop = getattr(self.transport, "stop", None)
            if callable(stop):
                stop()
            self.config = config
            self.transport = TelemetryTransport(config)
            self.transport.start()

    def enable_auto_instrumentation(self, config: AutoInstrumentationConfig) -> None:
        with self._lock:
            self.auto_config = config
            self.auto_enabled = True

    def disable_auto_instrumentation(self) -> None:
        with self._lock:
            self.auto_enabled = False

    @contextmanager
    def capture_context(self, source_layer: str, run_id: str | None = None) -> Iterator[None]:
        layers = self._active_layers.get()
        runs = self._run_stack.get()
        layer_token = self._active_layers.set((*layers, source_layer))
        run_token = self._run_stack.set((*runs, run_id) if run_id else runs)
        try:
            yield
        finally:
            self._active_layers.reset(layer_token)
            self._run_stack.reset(run_token)

    def active_layer(self) -> str | None:
        layers = self._active_layers.get()
        return layers[-1] if layers else None

    def active_run_id(self) -> str | None:
        runs = self._run_stack.get()
        return runs[-1] if runs else None

    def parent_run_id(self) -> str | None:
        runs = self._run_stack.get()
        return runs[-2] if len(runs) > 1 else None

    def in_framework_context(self) -> bool:
        layers = self._active_layers.get()
        return any(layer in {"langgraph", "langchain"} for layer in layers)

    def should_capture_layer(self, source_layer: str) -> bool:
        if not self.auto_enabled:
            return True
        if self.auto_config.capture_mode == "provider_only":
            return source_layer in {"openai", "anthropic"}
        if source_layer == "langgraph":
            return self.auto_config.instrument_langgraph
        if source_layer == "langchain":
            return self.auto_config.instrument_langchain
        if source_layer == "openai":
            return self.auto_config.instrument_openai
        if source_layer == "anthropic":
            return self.auto_config.instrument_anthropic
        return True

    def should_capture_provider(self, provider_layer: str) -> bool:
        if not self.should_capture_layer(provider_layer):
            return False
        if not self.auto_enabled:
            return True
        if self.auto_config.capture_mode == "provider_only":
            return True
        if self.auto_config.capture_mode == "all_layers":
            return True
        if self.auto_config.capture_mode == "hierarchical":
            if self.auto_config.provider_fallback_only and self.in_framework_context():
                return False
            return True
        return True

    def dedupe_fingerprint(
        self,
        join_key: str,
        provider: str,
        model: str,
        source_layer: str,
        run_id: str | None,
        start_time_ms: int,
    ) -> str:
        window = max(self.auto_config.dedupe_window_ms, 1)
        rounded = int(start_time_ms / window) * window
        raw = f"{join_key}|{provider}|{model}|{source_layer}|{run_id or ''}|{rounded}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def mark_primary_for_rollup(self, fingerprint: str, source_layer: str) -> bool:
        precedence = {"openai": 1, "anthropic": 1, "langchain": 2, "langgraph": 3}
        now = time.time()
        ttl = max(self.auto_config.dedupe_window_ms / 1000.0, 1.0)
        with self._dedupe_lock:
            current = self._dedupe_primary.get(fingerprint)
            if current and current[1] < now:
                current = None
                self._dedupe_primary.pop(fingerprint, None)

            if current is None:
                self._dedupe_primary[fingerprint] = (source_layer, now + ttl)
                return True

            current_layer, expires = current
            if precedence.get(source_layer, 0) > precedence.get(current_layer, 0):
                self._dedupe_primary[fingerprint] = (source_layer, expires)
                return True
            return source_layer == current_layer

    def should_emit_by_rate(self, source_layer: str) -> bool:
        if not self.auto_enabled:
            return True
        limit = max(self.auto_config.max_event_rate_per_sec, 1)
        now = time.time()
        with self._dedupe_lock:
            last = self._last_emit_ts.get(source_layer, 0.0)
            min_interval = 1.0 / float(limit)
            if now - last < min_interval:
                return False
            self._last_emit_ts[source_layer] = now
        return True


_runtime = RuntimeState()


def get_runtime() -> RuntimeState:
    return _runtime
