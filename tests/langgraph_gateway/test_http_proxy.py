import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
import tomllib

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import ClientDisconnect
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from zeroth.core.config.settings import LangGraphGatewaySettings
from zeroth.core.langgraph_gateway.headers import UpstreamCredentialUnavailableError
from zeroth.core.langgraph_gateway.transport import HTTPGatewayTransport
from zeroth.core.secrets.provider import EnvSecretProvider


async def upstream_fixture(request: Request) -> Response:
    case = request.path_params["case"]
    if case == "json":
        return Response(
            b'{"answer":42}',
            media_type="application/json",
            headers={"X-Upstream": "json"},
        )
    if case == "binary":
        return Response(b"\x00\xff\x10payload", media_type="application/octet-stream")
    if case == "empty":
        return Response(status_code=204, headers={"X-Upstream": "empty"})
    if case == "repeated":
        response = Response(b"repeated", media_type="text/plain")
        response.raw_headers.extend([(b"x-repeated", b"one"), (b"x-repeated", b"two")])
        return response
    if case == "unprocessable":
        return Response(
            b'{"detail":[{"msg":"invalid"}]}',
            status_code=422,
            media_type="application/json",
        )
    if case == "failure":
        return Response(b"agent server exploded", status_code=500, media_type="text/plain")
    if case == "echo":
        chunks = [chunk async for chunk in request.stream()]
        return Response(b"".join(chunks), media_type=request.headers.get("content-type"))

    async def hostile_sse() -> AsyncIterator[bytes]:
        for chunk in [b"ev", b"ent: to", b"ken\nda", b'ta: {"x":', b"1}\n", b"\n"]:
            yield chunk

    return StreamingResponse(hostile_sse(), media_type="text/event-stream")


def make_gateway(upstream: Starlette) -> tuple[Starlette, HTTPGatewayTransport]:
    settings = LangGraphGatewaySettings(
        enabled=True,
        upstream_url="http://agent-server/base",
        upstream_audience="fixture",
        deployment_ref="fixture-deployment",
    )
    transport = HTTPGatewayTransport(
        settings,
        EnvSecretProvider(),
        http_transport=httpx.ASGITransport(app=upstream),
    )

    async def proxy(request: Request) -> Response:
        return await transport.forward(request, tenant_id="tenant-a")

    return Starlette(routes=[Route("/{path:path}", proxy, methods=["GET", "POST"])]), transport


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "status", "body", "content_type"),
    [
        ("json", 200, b'{"answer":42}', "application/json"),
        ("binary", 200, b"\x00\xff\x10payload", "application/octet-stream"),
        ("empty", 204, b"", None),
        (
            "unprocessable",
            422,
            b'{"detail":[{"msg":"invalid"}]}',
            "application/json",
        ),
        ("failure", 500, b"agent server exploded", "text/plain; charset=utf-8"),
        ("sse", 200, b'event: token\ndata: {"x":1}\n\n', "text/event-stream; charset=utf-8"),
    ],
)
async def test_status_body_and_content_type_are_byte_transparent(case, status, body, content_type):
    upstream = Starlette(routes=[Route("/base/{case}", upstream_fixture)])
    gateway, transport = make_gateway(upstream)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway), base_url="http://gateway"
    ) as client:
        response = await client.get(f"/{case}")

    assert response.status_code == status
    assert response.content == body
    assert response.headers.get("content-type") == content_type
    await transport.aclose()


@pytest.mark.asyncio
async def test_repeated_end_to_end_response_headers_keep_order():
    upstream = Starlette(routes=[Route("/base/{case}", upstream_fixture)])
    gateway, transport = make_gateway(upstream)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway), base_url="http://gateway"
    ) as client:
        response = await client.get("/repeated")

    assert response.headers.get_list("x-repeated") == ["one", "two"]
    await transport.aclose()


@pytest.mark.asyncio
async def test_request_body_is_forwarded_as_a_stream():
    upstream = Starlette(routes=[Route("/base/{case}", upstream_fixture, methods=["POST"])])
    gateway, transport = make_gateway(upstream)

    async def body() -> AsyncIterator[bytes]:
        yield b"first-"
        yield b"second"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway), base_url="http://gateway"
    ) as client:
        response = await client.post(
            "/echo", content=body(), headers={"Content-Type": "application/x-stream"}
        )

    assert response.content == b"first-second"
    assert response.headers["content-type"] == "application/x-stream"
    await transport.aclose()


class CloseTrackedStream(httpx.AsyncByteStream):
    def __init__(self):
        self.closed = False

    async def __aiter__(self):
        yield b"first"
        yield b"second"

    async def aclose(self):
        self.closed = True


class SlowCloseTrackedStream(CloseTrackedStream):
    def __init__(self):
        super().__init__()
        self.close_started = asyncio.Event()
        self.allow_close = asyncio.Event()
        self.close_finished = asyncio.Event()

    async def aclose(self):
        self.close_started.set()
        await self.allow_close.wait()
        await super().aclose()
        self.close_finished.set()


def empty_request(
    path: str = "/stream",
    *,
    raw_path: bytes | None = b"/stream",
    headers: list[tuple[bytes, bytes]] | None = None,
    query_string: bytes = b"",
) -> Request:
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "query_string": query_string,
        "headers": headers or [],
        "server": ("gateway", 80),
        "client": ("test", 123),
    }
    if raw_path is not None:
        scope["raw_path"] = raw_path
    return Request(scope, receive)


@pytest.mark.asyncio
async def test_upstream_request_omits_httpx_semantic_default_headers():
    captured_headers = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.extend(request.headers.raw)
        return httpx.Response(204)

    settings = LangGraphGatewaySettings(
        enabled=True,
        upstream_url="http://agent-server",
        upstream_audience="fixture",
        deployment_ref="fixture-deployment",
    )
    transport = HTTPGatewayTransport(
        settings,
        EnvSecretProvider(),
        http_transport=httpx.MockTransport(handler),
    )
    response = await transport.forward(
        empty_request(headers=[(b"x-client", b"one"), (b"x-client", b"two")])
    )
    await response.body_iterator.aclose()

    names = {name.lower() for name, _ in captured_headers}
    assert names.isdisjoint({b"accept", b"accept-encoding", b"user-agent", b"connection"})
    assert [(name, value) for name, value in captured_headers if name == b"x-client"] == [
        (b"x-client", b"one"),
        (b"x-client", b"two"),
    ]
    assert (b"host", b"agent-server") in captured_headers
    await transport.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "raw_path", "query_string", "expected_raw_path"),
    [
        ("/café", None, b"q=caf%C3%A9", b"/base/caf%C3%A9?q=caf%C3%A9"),
        (
            "/already/escaped",
            b"/already%2Fescaped",
            b"value=%2F",
            b"/base/already%2Fescaped?value=%2F",
        ),
    ],
)
async def test_upstream_url_preserves_encoded_path_and_query(
    path, raw_path, query_string, expected_raw_path
):
    captured_raw_path = None

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_raw_path
        captured_raw_path = request.url.raw_path
        return httpx.Response(204)

    settings = LangGraphGatewaySettings(
        enabled=True,
        upstream_url="http://agent-server/base",
        upstream_audience="fixture",
        deployment_ref="fixture-deployment",
    )
    transport = HTTPGatewayTransport(
        settings,
        EnvSecretProvider(),
        http_transport=httpx.MockTransport(handler),
    )

    response = await transport.forward(
        empty_request(
            path,
            raw_path=raw_path,
            query_string=query_string,
        )
    )
    await response.body_iterator.aclose()

    assert captured_raw_path == expected_raw_path
    await transport.aclose()


@pytest.mark.asyncio
async def test_closing_downstream_body_closes_upstream_response():
    upstream_stream = CloseTrackedStream()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=upstream_stream)

    settings = LangGraphGatewaySettings(
        enabled=True,
        upstream_url="http://agent-server",
        upstream_audience="fixture",
        deployment_ref="fixture-deployment",
    )
    transport = HTTPGatewayTransport(
        settings,
        EnvSecretProvider(),
        http_transport=httpx.MockTransport(handler),
    )

    request = empty_request()

    response = await transport.forward(request)
    iterator = response.body_iterator
    assert await anext(iterator) == b"first"
    await iterator.aclose()

    assert upstream_stream.closed is True
    await transport.aclose()


@pytest.mark.asyncio
async def test_asgi_2_4_send_failure_closes_upstream_response():
    upstream_stream = CloseTrackedStream()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=upstream_stream)

    settings = LangGraphGatewaySettings(
        enabled=True,
        upstream_url="http://agent-server",
        upstream_audience="fixture",
        deployment_ref="fixture-deployment",
    )
    transport = HTTPGatewayTransport(
        settings,
        EnvSecretProvider(),
        http_transport=httpx.MockTransport(handler),
    )
    response = await transport.forward(empty_request())

    async def receive():
        await asyncio.Future()

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            raise OSError("client disconnected")

    with pytest.raises(ClientDisconnect):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)

    assert upstream_stream.closed is True
    await transport.aclose()


@pytest.mark.asyncio
async def test_asgi_2_3_cancellation_closes_upstream_response():
    upstream_stream = CloseTrackedStream()
    send_started = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=upstream_stream)

    settings = LangGraphGatewaySettings(
        enabled=True,
        upstream_url="http://agent-server",
        upstream_audience="fixture",
        deployment_ref="fixture-deployment",
    )
    transport = HTTPGatewayTransport(
        settings,
        EnvSecretProvider(),
        http_transport=httpx.MockTransport(handler),
    )
    response = await transport.forward(empty_request())

    async def receive():
        await asyncio.Future()

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            send_started.set()
            await asyncio.Future()

    response_task = asyncio.create_task(
        response({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send)
    )
    await send_started.wait()
    response_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await response_task

    assert upstream_stream.closed is True
    await transport.aclose()


@pytest.mark.asyncio
async def test_cancellation_propagates_while_shielded_upstream_close_finishes():
    upstream_stream = SlowCloseTrackedStream()
    send_started = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=upstream_stream)

    settings = LangGraphGatewaySettings(
        enabled=True,
        upstream_url="http://agent-server",
        upstream_audience="fixture",
        deployment_ref="fixture-deployment",
    )
    transport = HTTPGatewayTransport(
        settings,
        EnvSecretProvider(),
        http_transport=httpx.MockTransport(handler),
    )
    response = await transport.forward(empty_request())

    async def receive():
        await asyncio.Future()

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            send_started.set()
            await asyncio.Future()

    response_task = asyncio.create_task(
        response({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send)
    )
    await send_started.wait()
    response_task.cancel()
    await upstream_stream.close_started.wait()
    await asyncio.sleep(0)
    cancellation_propagated = response_task.done()
    transport_close_task = asyncio.create_task(transport.aclose())
    await asyncio.sleep(0)
    transport_waited_for_upstream = not transport_close_task.done()
    upstream_stream.allow_close.set()
    with pytest.raises(asyncio.CancelledError):
        await response_task
    await upstream_stream.close_finished.wait()
    await transport_close_task

    assert cancellation_propagated is True
    assert transport_waited_for_upstream is True
    assert upstream_stream.closed is True


@pytest.mark.asyncio
async def test_transport_close_waits_for_an_in_progress_response_close():
    upstream_stream = SlowCloseTrackedStream()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=upstream_stream)

    settings = LangGraphGatewaySettings(
        enabled=True,
        upstream_url="http://agent-server",
        upstream_audience="fixture",
        deployment_ref="fixture-deployment",
    )
    transport = HTTPGatewayTransport(
        settings,
        EnvSecretProvider(),
        http_transport=httpx.MockTransport(handler),
    )
    response = await transport.forward(empty_request())
    iterator = response.body_iterator
    assert await anext(iterator) == b"first"
    iterator_close_task = asyncio.create_task(iterator.aclose())
    await upstream_stream.close_started.wait()

    transport_close_task = asyncio.create_task(transport.aclose())
    try:
        await asyncio.wait_for(asyncio.shield(transport_close_task), timeout=0.01)
    except TimeoutError:
        transport_waited = True
    else:
        transport_waited = False
    upstream_stream.allow_close.set()
    await iterator_close_task
    await transport_close_task

    assert transport_waited is True
    assert upstream_stream.closed is True


@pytest.mark.asyncio
async def test_missing_credential_fails_before_opening_upstream_connection():
    connection_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal connection_count
        connection_count += 1
        return httpx.Response(200)

    settings = LangGraphGatewaySettings(
        enabled=True,
        upstream_url="http://agent-server",
        upstream_audience="fixture",
        deployment_ref="fixture-deployment",
        upstream_credential_ref="agent.api-key",
    )
    transport = HTTPGatewayTransport(
        settings,
        EnvSecretProvider(),
        http_transport=httpx.MockTransport(handler),
    )

    request = empty_request("/health", raw_path=b"/health")

    with pytest.raises(UpstreamCredentialUnavailableError) as caught:
        await transport.forward(request, tenant_id="tenant-a")

    assert caught.value.code == "zeroth.upstream_credential_unavailable"
    assert connection_count == 0
    await transport.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("credential_header", "credential", "request_headers"),
    [
        ("Authorization", "secret\r\nX-Injected: yes", []),
        ("Host", "secret", []),
        (
            "X-Upstream-Credential",
            "secret",
            [(b"connection", b"X-Upstream-Credential")],
        ),
    ],
)
async def test_hostile_credential_configuration_never_connects(
    credential_header, credential, request_headers
):
    connection_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal connection_count
        connection_count += 1
        return httpx.Response(200)

    settings = LangGraphGatewaySettings(
        enabled=True,
        upstream_url="http://agent-server",
        upstream_audience="fixture",
        deployment_ref="fixture-deployment",
        upstream_credential_ref="agent.api-key",
        upstream_credential_header=credential_header,
    )
    transport = HTTPGatewayTransport(
        settings,
        EnvSecretProvider({"AGENT_API_KEY": credential}),
        http_transport=httpx.MockTransport(handler),
    )

    with pytest.raises(UpstreamCredentialUnavailableError) as caught:
        await transport.forward(empty_request(headers=request_headers))

    assert caught.value.code == "zeroth.upstream_credential_unavailable"
    assert credential not in str(caught.value)
    assert credential not in repr(caught.value)
    assert connection_count == 0
    await transport.aclose()


def test_all_extra_includes_langgraph_gateway_runtime():
    project = tomllib.loads(Path("pyproject.toml").read_text())

    assert "zeroth-core[langgraph-gateway]" in project["project"]["optional-dependencies"]["all"]
