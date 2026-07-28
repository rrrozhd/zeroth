from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any

import httpx
import pytest
import uvicorn
from starlette.requests import Request

from tests.langgraph_gateway.conformance.harness import create_gateway_app
from zeroth.core.config.settings import LangGraphGatewaySettings
from zeroth.core.langgraph_gateway.models import GatewayEventStatus
from zeroth.core.langgraph_gateway.transport import HTTPGatewayTransport
from zeroth.core.secrets.provider import EnvSecretProvider


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request(path: str = "/ok", receive=None) -> Request:
    sent = False

    async def default_receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "server": ("gateway", 80),
            "client": ("test", 1),
        },
        receive or default_receive,
    )


def _settings(upstream_url: str) -> LangGraphGatewaySettings:
    return LangGraphGatewaySettings(
        enabled=True,
        upstream_url=upstream_url,
        upstream_audience="resilience",
        deployment_ref="resilience",
        connect_timeout_seconds=0.1,
        read_timeout_seconds=0.1,
        write_timeout_seconds=0.1,
        pool_timeout_seconds=0.1,
    )


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_real_socket_disconnect_closes_upstream_and_emits_disconnected() -> None:
    upstream_closed = asyncio.Event()

    async def upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(
                b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n"
                b"transfer-encoding: chunked\r\n\r\n"
            )
            await writer.drain()
            for index in range(10_000):
                payload = f"data: {index}\n\n".encode()
                writer.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n")
                await writer.drain()
                await asyncio.sleep(0.005)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            upstream_closed.set()
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()

    upstream_server = await asyncio.start_server(upstream, "127.0.0.1", 0)
    upstream_port = upstream_server.sockets[0].getsockname()[1]
    sink = _RecordingSink()
    app, transport = create_gateway_app(f"http://127.0.0.1:{upstream_port}", event_sink=sink)
    gateway_port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=gateway_port,
            log_level="error",
            access_log=False,
            date_header=False,
            server_header=False,
        )
    )
    server_task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)

    reader, writer = await asyncio.open_connection("127.0.0.1", gateway_port)
    writer.write(b"GET /ok HTTP/1.1\r\nHost: gateway\r\nConnection: close\r\n\r\n")
    await writer.drain()
    assert b"200 OK" in await reader.readuntil(b"\r\n\r\n")
    assert await reader.read(32)
    writer.close()
    await writer.wait_closed()

    await asyncio.wait_for(upstream_closed.wait(), timeout=2)
    for _ in range(100):
        if sink.events:
            break
        await asyncio.sleep(0.01)
    assert sink.events[-1].status == GatewayEventStatus.CLIENT_DISCONNECT
    assert not transport._open_responses

    server.should_exit = True
    await asyncio.wait_for(server_task, timeout=5)
    upstream_server.close()
    await upstream_server.wait_closed()


@pytest.mark.asyncio
async def test_connect_and_midstream_timeouts_leave_no_open_response() -> None:
    async def connect_timeout(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connect timed out")

    settings = _settings("http://upstream")
    transport = HTTPGatewayTransport(
        settings,
        EnvSecretProvider(),
        http_transport=httpx.MockTransport(connect_timeout),
    )
    with pytest.raises(httpx.ConnectTimeout):
        await transport.forward(_request())
    assert not transport._open_responses
    await transport.aclose()

    async def read_timeout_before_headers(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out before response headers")

    transport = HTTPGatewayTransport(
        settings,
        EnvSecretProvider(),
        http_transport=httpx.MockTransport(read_timeout_before_headers),
    )
    with pytest.raises(httpx.ReadTimeout, match="before response headers"):
        await transport.forward(_request())
    assert not transport._open_responses
    await transport.aclose()

    class TimeoutStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.close_calls = 0

        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"already-delivered"
            raise httpx.ReadTimeout("midstream timeout")

        async def aclose(self) -> None:
            self.close_calls += 1

    stream = TimeoutStream()

    async def midstream(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    transport = HTTPGatewayTransport(
        settings,
        EnvSecretProvider(),
        http_transport=httpx.MockTransport(midstream),
    )
    response = await transport.forward(_request())
    iterator = response.body_iterator.__aiter__()
    assert await anext(iterator) == b"already-delivered"
    with pytest.raises(httpx.ReadTimeout):
        await anext(iterator)
    assert stream.close_calls == 1
    assert not transport._open_responses
    await transport.aclose()


@pytest.mark.asyncio
async def test_cancellation_during_upstream_connect_leaves_no_inflight_or_open_response() -> None:
    connect_started = asyncio.Event()
    connect_cancelled = asyncio.Event()

    async def connecting(_: httpx.Request) -> httpx.Response:
        connect_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            connect_cancelled.set()
            raise

    transport = HTTPGatewayTransport(
        _settings("http://upstream"),
        EnvSecretProvider(),
        http_transport=httpx.MockTransport(connecting),
    )
    forward = asyncio.create_task(transport.forward(_request()))
    await connect_started.wait()
    forward.cancel()
    with pytest.raises(asyncio.CancelledError):
        await forward

    assert connect_cancelled.is_set()
    assert not transport._open_responses
    await transport.aclose()


@pytest.mark.asyncio
async def test_observer_exception_preserves_delivered_bytes_and_closes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import zeroth.core.langgraph_gateway.proxy as proxy_module
    from tests.langgraph_gateway.test_http_proxy import (
        AllowBudget,
        AllowPolicy,
        RecordingEventSink,
        authenticated_empty_request,
        supported_compatibility,
    )
    from zeroth.core.langgraph_gateway.context import ReservedContextCodec
    from zeroth.core.langgraph_gateway.proxy import GatewayProxy
    from zeroth.core.signing import EnvHmacSigner

    finish_calls = 0

    class ExplodingObserver:
        identifiers: dict[str, str] = {}
        output_sha256 = None
        output_size_bytes = 0

        def __init__(self, *_: Any, **__: Any) -> None:
            self.seen = 0

        def observe(self, chunk: bytes) -> bytes:
            self.seen += 1
            self.output_size_bytes += len(chunk)
            if self.seen == 2:
                raise RuntimeError("observer parser failed")
            return chunk

        def finish(self) -> None:
            nonlocal finish_calls
            finish_calls += 1

    class CloseOnceStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.close_calls = 0

        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"already-delivered"
            yield b"parser-trigger"

        async def aclose(self) -> None:
            self.close_calls += 1

    stream = CloseOnceStream()

    async def upstream(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream, headers={"content-type": "application/json"})

    monkeypatch.setattr(proxy_module, "TeeObserver", ExplodingObserver)
    settings = _settings("http://upstream")
    transport = HTTPGatewayTransport(
        settings,
        EnvSecretProvider(),
        http_transport=httpx.MockTransport(upstream),
    )
    sink = RecordingEventSink()
    proxy = GatewayProxy(
        settings=settings,
        transport=transport,
        context_codec=ReservedContextCodec(
            EnvHmacSigner(key_id="resilience", keys={"resilience": b"key"})
        ),
        policy_guard=AllowPolicy(),
        budget_checker=AllowBudget(),
        compatibility=supported_compatibility(),
        event_sink=sink,
    )
    response = await proxy.handle_http(authenticated_empty_request("/ok"))
    iterator = response.body_iterator.__aiter__()
    assert await anext(iterator) == b"already-delivered"
    with pytest.raises(RuntimeError, match="observer parser failed"):
        await anext(iterator)

    assert finish_calls == 1
    assert stream.close_calls == 1
    assert sink.events[-1].status == GatewayEventStatus.UPSTREAM_ERROR
    assert not transport._open_responses
    await transport.aclose()


@pytest.mark.asyncio
async def test_slow_response_and_upload_are_pull_bounded() -> None:
    class PullStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.produced = 0

        async def __aiter__(self) -> AsyncIterator[bytes]:
            for _ in range(256):
                self.produced += 1
                yield b"x" * 1024

    response_stream = PullStream()

    async def response_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=response_stream)

    transport = HTTPGatewayTransport(
        _settings("http://upstream"),
        EnvSecretProvider(),
        http_transport=httpx.MockTransport(response_handler),
    )
    response = await transport.forward(_request())
    iterator = response.body_iterator.__aiter__()
    assert await anext(iterator) == b"x" * 1024
    assert response_stream.produced == 1
    await iterator.aclose()
    await transport.aclose()

    produced = 0
    consumed = 0
    max_lead = 0

    async def receive() -> dict[str, Any]:
        nonlocal produced
        if produced == 256:
            return {"type": "http.request", "body": b"", "more_body": False}
        produced += 1
        return {"type": "http.request", "body": b"y" * 1024, "more_body": True}

    class SlowUploadTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal consumed, max_lead
            async for chunk in request.stream:
                if not chunk:
                    continue
                consumed += 1
                max_lead = max(max_lead, produced - consumed)
                await asyncio.sleep(0)
                assert len(chunk) <= 1024
            return httpx.Response(204)

    transport = HTTPGatewayTransport(
        _settings("http://upstream"),
        EnvSecretProvider(),
        http_transport=SlowUploadTransport(),
    )
    response = await transport.forward(_request(receive=receive))
    assert response.status_code == 204
    assert max_lead <= 1
    assert produced == consumed == 256
    await response.body_iterator.aclose()
    await transport.aclose()
