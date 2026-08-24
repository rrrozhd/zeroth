"""TelemetryTransport in-process ASGI dispatch seam (audit P0, write path).

Unit-level proof that the transport delivers cost events over httpx.ASGITransport
when an ``asgi_app`` is configured, still uses the plain httpx.Client path when it
is not (so the monkeypatched-sync invariant is untouched), and applies the same
retry/drop bookkeeping to a 4xx/5xx from the in-process app as to an HTTP error.
"""

from __future__ import annotations

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from zeroth.econ.instrumentation.config import InstrumentationConfig
from zeroth.econ.instrumentation.schemas import ExecutionEvent
from zeroth.econ.instrumentation.transport import TelemetryTransport


def _recorder_app(recorded: list[dict], *, status: int = 200) -> Starlette:
    async def _ingest(request):
        recorded.append(await request.json())
        return JSONResponse({"ok": True}, status_code=status)

    return Starlette(
        routes=[Route("/instrumentation/executions", _ingest, methods=["POST"])]
    )


def _config(**overrides) -> InstrumentationConfig:
    base = {
        "base_url": "http://regulus.internal",
        "request_timeout_s": 5.0,
        "enabled": True,
        "max_retries": 2,
    }
    base.update(overrides)
    return InstrumentationConfig(**base)


def _event() -> ExecutionEvent:
    return ExecutionEvent(capability_id="c", implementation_id="i")


def test_asgi_dispatch_delivers_event_in_process() -> None:
    recorded: list[dict] = []
    transport = TelemetryTransport(_config(), asgi_app=_recorder_app(recorded))
    transport.enqueue_execution(_event())
    transport.flush_once()

    assert len(recorded) == 1
    assert recorded[0]["capability_id"] == "c"
    assert recorded[0]["implementation_id"] == "i"
    assert transport.dropped_events == 0
    assert transport.queue_size() == 0


def test_asgi_error_status_requeues_like_http() -> None:
    # A 5xx from the in-process app must be treated as a failure (requeued), the
    # same as an HTTP-path error — never a silent success.
    recorded: list[dict] = []
    transport = TelemetryTransport(_config(), asgi_app=_recorder_app(recorded, status=500))
    transport.enqueue_execution(_event())
    transport.flush_once()

    assert len(recorded) == 1  # the app was reached
    assert transport.dropped_events == 0  # first failure -> requeue, not drop
    assert transport.queue_size() == 1  # scheduled for retry


def test_no_asgi_app_uses_sync_httpx_client(monkeypatch) -> None:
    # Guard against an accidental default flip: with asgi_app unset the transport
    # must still route through httpx.Client, not the ASGI path.
    posted: list[str] = []

    class _FakeResponse:
        status_code = 200

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, json):
            posted.append(url)
            return _FakeResponse()

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    transport = TelemetryTransport(_config())
    assert transport._asgi_app is None
    transport.enqueue_execution(_event())
    transport.flush_once()

    assert posted == ["http://regulus.internal/instrumentation/executions"]
    assert transport.dropped_events == 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
