from collections.abc import AsyncIterator

import httpx
import pytest
from starlette.applications import Starlette
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

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "path": "/stream",
            "raw_path": b"/stream",
            "query_string": b"",
            "headers": [],
            "server": ("gateway", 80),
            "client": ("test", 123),
        },
        receive,
    )

    response = await transport.forward(request)
    iterator = response.body_iterator
    assert await anext(iterator) == b"first"
    await iterator.aclose()

    assert upstream_stream.closed is True
    await transport.aclose()


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

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "path": "/health",
            "raw_path": b"/health",
            "query_string": b"",
            "headers": [],
            "server": ("gateway", 80),
            "client": ("test", 123),
        },
        receive,
    )

    with pytest.raises(UpstreamCredentialUnavailableError) as caught:
        await transport.forward(request, tenant_id="tenant-a")

    assert caught.value.code == "zeroth.upstream_credential_unavailable"
    assert connection_count == 0
    await transport.aclose()
