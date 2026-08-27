from __future__ import annotations

import logging
import subprocess
import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI

from zeroth.econ.instrumentation import client
from zeroth.econ.instrumentation.config import InstrumentationConfig
from zeroth.econ.instrumentation.integrations import anthropic, langchain, openai
from zeroth.econ.instrumentation.runtime import RuntimeState
from zeroth.econ.instrumentation.schemas import ExecutionEvent
from zeroth.econ.instrumentation.transport import TelemetryTransport


def test_cold_import_starts_no_threads_or_sockets() -> None:
    script = """
import socket
import threading
before = {thread.ident for thread in threading.enumerate()}
original_connect = socket.socket.connect
def fail_connect(self, *args, **kwargs):
    raise AssertionError('socket opened during import')
socket.socket.connect = fail_connect
import zeroth.econ.instrumentation
after = {thread.ident for thread in threading.enumerate()}
assert after == before, (before, after)
socket.socket.connect = original_connect
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_same_millisecond_execution_ids_are_distinct(monkeypatch) -> None:
    fixed = datetime(2026, 1, 1, tzinfo=UTC)

    class FrozenDateTime:
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr(client, "datetime", FrozenDateTime)
    monkeypatch.setattr(openai, "datetime", FrozenDateTime)
    monkeypatch.setattr(anthropic, "datetime", FrozenDateTime)
    monkeypatch.setattr(client, "track_execution", lambda event: None)

    with client.with_instrumentation("cap", "impl") as first:
        pass
    with client.with_instrumentation("cap", "impl") as second:
        pass
    openai_ids = {
        openai._build_event("cap", "impl", 1, "model", {}).execution_id for _ in range(2)
    }
    anthropic_ids = {
        anthropic._build_event("cap", "impl", 1, "model", {}).execution_id
        for _ in range(2)
    }
    langchain_ids = {langchain._new_run_id() for _ in range(2)}

    assert first["execution_id"] != second["execution_id"]
    assert len(openai_ids) == 2
    assert len(anthropic_ids) == 2
    assert len(langchain_ids) == 2


def test_join_key_cache_is_bounded(monkeypatch) -> None:
    runtime = SimpleNamespace(
        auto_enabled=False,
        auto_config=SimpleNamespace(strict_join_key=False),
        transport=SimpleNamespace(enqueue_execution=lambda event: None),
    )
    monkeypatch.setattr(client, "get_runtime", lambda: runtime)
    client._execution_join_key.clear()

    for index in range(client._MAX_EXECUTION_JOIN_KEYS + 1):
        client.track_execution(
            ExecutionEvent(
                execution_id=f"exec-{index}",
                capability_id="cap",
                implementation_id="impl",
            )
        )

    assert len(client._execution_join_key) == client._MAX_EXECUTION_JOIN_KEYS
    assert "exec-0" not in client._execution_join_key


def test_noop_configure_keeps_transport(monkeypatch) -> None:
    class FakeTransport:
        def __init__(self, config):
            self.config = config
            self.started = False
            self.stopped = False

        def start(self):
            self.started = True

        def stop(self):
            self.stopped = True

    monkeypatch.setattr("zeroth.econ.instrumentation.runtime.TelemetryTransport", FakeTransport)
    state = RuntimeState()
    original = state.transport
    state.configure(state.config.model_copy())
    assert state.transport is original
    assert not original.stopped


def test_buffer_and_final_retry_drops_are_logged(monkeypatch, caplog) -> None:
    config = InstrumentationConfig(buffer_max_events=1, max_retries=1)
    transport = TelemetryTransport(config)
    event = ExecutionEvent(capability_id="cap", implementation_id="impl")

    with caplog.at_level(logging.WARNING):
        transport.enqueue_execution(event)
        transport.enqueue_execution(event.model_copy(update={"execution_id": "second"}))

        class FailingClient:
            def __init__(self, **kwargs: Any):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args: Any):
                return None

            def post(self, *args: Any, **kwargs: Any):
                return SimpleNamespace(status_code=500)

        monkeypatch.setattr("zeroth.econ.instrumentation.transport.httpx.Client", FailingClient)
        transport.flush_once()

    assert transport.dropped_events == 2
    assert caplog.text.count("instrumentation event dropped") == 2
    assert "/instrumentation/executions" in caplog.text


def test_in_process_transport_delivers_without_opening_a_socket(monkeypatch) -> None:
    received: list[dict[str, Any]] = []
    app = FastAPI()

    @app.post("/v1/instrumentation/executions")
    async def receive_execution(payload: dict[str, Any]) -> dict[str, str]:
        received.append(payload)
        return {"status": "inserted"}

    config = InstrumentationConfig(base_url="http://regulus.internal/v1", max_retries=1)
    transport = TelemetryTransport(config, _asgi_app=app)
    monkeypatch.setattr(
        "zeroth.econ.instrumentation.transport.httpx.Client",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("opened a network client")),
    )

    transport.enqueue_execution(
        ExecutionEvent(capability_id="cap", implementation_id="impl")
    )
    transport.flush_once()

    assert len(received) == 1
    assert received[0]["capability_id"] == "cap"
    assert transport.queue_size() == 0
