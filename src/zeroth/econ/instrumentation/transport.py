from __future__ import annotations

import asyncio
import threading
import time
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from zeroth.econ.instrumentation.config import InstrumentationConfig
from zeroth.econ.instrumentation.schemas import ExecutionEvent, OutcomeEvent

logger = logging.getLogger(__name__)


@dataclass
class _QueuedEvent:
    endpoint: str
    payload: dict[str, Any]
    attempts: int = 0
    next_attempt_at: float = 0.0


class TelemetryTransport:
    def __init__(
        self,
        config: InstrumentationConfig,
        headers_provider: Callable[[], dict[str, str]] | None = None,
        asgi_app: Any | None = None,
    ):
        # headers_provider (Zeroth vendor addition): called once per flush to
        # obtain fresh request headers (e.g. a short-lived auth token). See
        # VENDOR.md.
        self._headers_provider = headers_provider
        # asgi_app (Zeroth vendor addition): when set, cost-event WRITES are
        # dispatched in-process into this ASGI app (the bundled ``/regulus``
        # mount) instead of over an external HTTP socket — the exact mirror of
        # the BudgetEnforcer READ seam. A default bundled deploy points
        # ``base_url`` at the external ``localhost:8000`` topology where nothing
        # listens, so without this every event POSTs to a refused socket, is
        # retried, and dropped — spend stays 0 and budget caps never trip.
        self._asgi_app = asgi_app
        self.config = config
        self._queue: deque[_QueuedEvent] = deque()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.dropped_events = 0

    def start(self) -> None:
        if not self.config.enabled:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def enqueue_execution(self, event: ExecutionEvent) -> None:
        self._enqueue("/instrumentation/executions", event.model_dump(mode="json"))

    async def aenqueue_execution(self, event: ExecutionEvent) -> None:
        self.enqueue_execution(event)

    def enqueue_outcome(self, event: OutcomeEvent) -> None:
        self._enqueue("/instrumentation/outcomes", event.model_dump(mode="json"))

    async def aenqueue_outcome(self, event: OutcomeEvent) -> None:
        self.enqueue_outcome(event)

    def queue_size(self) -> int:
        with self._lock:
            return len(self._queue)

    def _enqueue(self, endpoint: str, payload: dict[str, Any]) -> None:
        if not self.config.enabled:
            return
        with self._lock:
            if len(self._queue) >= self.config.buffer_max_events:
                dropped = self._queue.popleft()
                self._record_drop(dropped, "buffer_overflow")
            self._queue.append(_QueuedEvent(endpoint=endpoint, payload=payload, next_attempt_at=time.time()))

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.flush_once()
            except Exception:
                logger.exception("instrumentation flush failed")
            self._stop.wait(self.config.flush_interval_ms / 1000.0)

    def flush_once(self) -> None:
        ready_events: list[_QueuedEvent] = []
        now = time.time()
        with self._lock:
            remaining = deque()
            while self._queue:
                evt = self._queue.popleft()
                if evt.next_attempt_at <= now:
                    ready_events.append(evt)
                else:
                    remaining.append(evt)
            self._queue = remaining

        if not ready_events:
            return

        headers = self._headers_provider() if self._headers_provider is not None else None
        if self._asgi_app is not None:
            failed = self._flush_asgi(ready_events, headers)
        else:
            failed = self._flush_http(ready_events, headers)

        if failed:
            with self._lock:
                for evt in failed:
                    if len(self._queue) >= self.config.buffer_max_events:
                        dropped = self._queue.popleft()
                        self._record_drop(dropped, "retry_buffer_overflow")
                    self._queue.append(evt)

    def _schedule_retry(self, evt: _QueuedEvent, failed: list[_QueuedEvent]) -> None:
        # Shared retry/drop bookkeeping so the HTTP and ASGI delivery paths cannot
        # drift in their backoff or drop semantics.
        if evt.attempts + 1 < self.config.max_retries:
            next_attempt = evt.attempts + 1
            backoff = min(2 ** next_attempt, 30)
            failed.append(
                _QueuedEvent(
                    endpoint=evt.endpoint,
                    payload=evt.payload,
                    attempts=next_attempt,
                    next_attempt_at=time.time() + backoff,
                )
            )
        else:
            self._record_drop(evt, "retry_exhausted")

    def _flush_http(self, ready_events: list[_QueuedEvent], headers: dict[str, str] | None) -> list[_QueuedEvent]:
        # External-HTTP delivery (unchanged from the original flush): used whenever
        # no in-process asgi_app is configured, so the monkeypatched-httpx.Client
        # invariant path is byte-for-byte identical.
        failed: list[_QueuedEvent] = []
        with httpx.Client(timeout=self.config.request_timeout_s, headers=headers) as client:
            for evt in ready_events:
                try:
                    resp = client.post(f"{self.config.base_url.rstrip('/')}{evt.endpoint}", json=evt.payload)
                    if resp.status_code >= 400:
                        raise RuntimeError(f"status {resp.status_code}")
                except Exception:
                    self._schedule_retry(evt, failed)
        return failed

    def _flush_asgi(self, ready_events: list[_QueuedEvent], headers: dict[str, str] | None) -> list[_QueuedEvent]:
        # In-process delivery into the bundled control plane over httpx.ASGITransport
        # (mirror of BudgetEnforcer's READ seam). The flush is synchronous (called
        # from the daemon thread AND from RegulusClient.stop(), which the async
        # lifespan shutdown awaits from *inside* a running loop). Driving the
        # coroutine on a throwaway loop in a helper thread makes it safe from any
        # caller context: a bare run_until_complete would raise "this event loop is
        # already running" when stop() flushes during async shutdown.
        failed: list[_QueuedEvent] = []

        async def _run() -> None:
            transport = httpx.ASGITransport(app=self._asgi_app)
            async with httpx.AsyncClient(
                transport=transport, timeout=self.config.request_timeout_s, headers=headers
            ) as client:
                for evt in ready_events:
                    try:
                        resp = await client.post(
                            f"{self.config.base_url.rstrip('/')}{evt.endpoint}", json=evt.payload
                        )
                        if resp.status_code >= 400:
                            raise RuntimeError(f"status {resp.status_code}")
                    except Exception:
                        self._schedule_retry(evt, failed)

        error: list[BaseException] = []

        def _drive() -> None:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(_run())
            except BaseException as exc:  # noqa: BLE001 — surfaced to the caller below
                error.append(exc)
            finally:
                loop.close()

        worker = threading.Thread(target=_drive, name="ecp-telemetry-asgi-flush", daemon=True)
        worker.start()
        worker.join()
        if error:
            # A transport-level failure (not a per-event 4xx/5xx, which _run already
            # converts to retries) — let the outer _run loop log it like any flush error.
            raise error[0]
        return failed

    def _record_drop(self, event: _QueuedEvent, reason: str) -> None:
        self.dropped_events += 1
        logger.warning(
            "instrumentation event dropped endpoint=%s reason=%s total=%d",
            event.endpoint,
            reason,
            self.dropped_events,
        )
