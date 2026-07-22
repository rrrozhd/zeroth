import asyncio
from collections.abc import AsyncIterator
import hashlib
import json
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
from zeroth.core.econ.budget import BudgetCheckResult
from zeroth.core.identity import AuthMethod, AuthenticatedPrincipal, ServiceRole
from zeroth.core.langgraph_gateway.compatibility import CompatibilityResult
from zeroth.core.langgraph_gateway.context import ReservedContextCodec
from zeroth.core.langgraph_gateway.headers import UpstreamCredentialUnavailableError
from zeroth.core.langgraph_gateway.inventory import classify_endpoint
from zeroth.core.langgraph_gateway.models import CompatibilityStatus
from zeroth.core.langgraph_gateway.proxy import GatewayProxy
from zeroth.core.langgraph_gateway.transport import HTTPGatewayTransport
from zeroth.core.policy.models import RunAdmissionResult
from zeroth.core.secrets.provider import EnvSecretProvider
from zeroth.core.signing import EnvHmacSigner, NullSigner


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
        ("/a?b", None, b"query=separate", b"/base/a%3Fb?query=separate"),
        ("/a#b", None, b"", b"/base/a%23b"),
        ("/a%b", None, b"", b"/base/a%25b"),
        ("/a/b", None, b"", b"/base/a/b"),
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
async def test_shutdown_waits_for_in_flight_sends_and_closes_late_responses():
    send_started = [asyncio.Event(), asyncio.Event()]
    release_sends = asyncio.Event()
    streams = [CloseTrackedStream(), CloseTrackedStream()]
    send_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal send_count
        index = send_count
        send_count += 1
        send_started[index].set()
        await release_sends.wait()
        return httpx.Response(200, stream=streams[index])

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
    forward_tasks = [
        asyncio.create_task(transport.forward(empty_request(f"/send-{index}")))
        for index in range(2)
    ]
    await asyncio.gather(*(event.wait() for event in send_started))

    close_task = asyncio.create_task(transport.aclose())
    await asyncio.sleep(0)
    shutdown_waited = not close_task.done()
    with pytest.raises(RuntimeError, match="^HTTP gateway transport is closed$"):
        await transport.forward(empty_request("/late-send"))
    release_sends.set()
    results = await asyncio.gather(*forward_tasks, return_exceptions=True)
    await close_task

    assert shutdown_waited is True
    assert all(
        isinstance(result, RuntimeError) and str(result) == "HTTP gateway transport is closed"
        for result in results
    )
    assert all(stream.closed for stream in streams)
    assert send_count == 2


@pytest.mark.asyncio
async def test_forward_after_shutdown_fails_before_network():
    connection_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal connection_count
        connection_count += 1
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
    await transport.aclose()

    with pytest.raises(RuntimeError, match="^HTTP gateway transport is closed$"):
        await transport.forward(empty_request())

    assert connection_count == 0


@pytest.mark.asyncio
async def test_cancellation_after_send_does_not_block_shutdown_or_leak_response():
    handler_started = asyncio.Event()
    handler_returning = asyncio.Event()
    release_send = asyncio.Event()
    stream = CloseTrackedStream()

    async def handler(request: httpx.Request) -> httpx.Response:
        handler_started.set()
        await release_send.wait()
        handler_returning.set()
        return httpx.Response(200, stream=stream)

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
    forward_task = asyncio.create_task(transport.forward(empty_request()))
    await handler_started.wait()

    async with transport._lifecycle:
        release_send.set()
        await handler_returning.wait()
        await asyncio.sleep(0)
        forward_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await forward_task
    await asyncio.wait_for(transport.aclose(), timeout=0.1)

    assert stream.closed is True


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
    await asyncio.wait_for(transport.aclose(), timeout=0.1)


def test_all_extra_includes_langgraph_gateway_runtime():
    project = tomllib.loads(Path("pyproject.toml").read_text())

    assert "zeroth-core[langgraph-gateway]" in project["project"]["optional-dependencies"]["all"]


class AllowPolicy:
    def evaluate_run_admission(self, request):
        return RunAdmissionResult(allowed=True, policy_version="sha256:policy")


class DenyPolicy:
    def evaluate_run_admission(self, request):
        return RunAdmissionResult(
            allowed=False,
            policy_version="sha256:policy",
            reason="zeroth.policy_denied",
        )


class AllowBudget:
    async def check_budget_status(self, tenant_id):
        return BudgetCheckResult(allowed=True, spend_usd=1, cap_usd=10)


def supported_compatibility() -> CompatibilityResult:
    return CompatibilityResult(
        tested_langgraph_versions=("1.2.9",),
        tested_agent_server_versions=("0.11.1",),
        detected_agent_server_version="0.11.1",
        openapi_fingerprint="sha256:openapi",
        status=CompatibilityStatus.SUPPORTED,
    )


def principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        subject="user-7",
        auth_method=AuthMethod.API_KEY,
        roles=[ServiceRole.OPERATOR],
        tenant_id="tenant-a",
    )


def governed_request(
    body: bytes,
    *,
    path: str = "/threads/thread-4/runs/stream",
    receive_hook=None,
) -> Request:
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        if receive_hook is not None:
            receive_hook()
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "server": ("gateway", 80),
            "client": ("test", 123),
            "state": {"principal": principal()},
        },
        receive,
    )


def authenticated_empty_request(path: str) -> Request:
    request = empty_request(path, raw_path=path.encode())
    request.state.principal = principal()
    return request


@pytest.mark.asyncio
async def test_governed_pipeline_order_and_signed_claims_are_exact():
    order = []
    captured = {}

    class RecordingClassifier:
        async def classify(self, payload):
            order.append("admission")
            return "internal"

    class RecordingSigner:
        def __init__(self):
            self.delegate = EnvHmacSigner(key_id="gateway", keys={"gateway": b"shared-key"})

        def algorithm(self):
            return self.delegate.algorithm()

        def key_id(self):
            return self.delegate.key_id()

        def sign(self, payload):
            order.append("signed injection")
            return self.delegate.sign(payload)

        def verify(self, payload, signature, key_id):
            return self.delegate.verify(payload, signature, key_id)

    class RecordingSecrets(EnvSecretProvider):
        async def resolve_secret_async(self, logical_name, **kwargs):
            order.append("credential replacement")
            return "upstream-secret"

    async def upstream(request):
        order.append("transport")
        captured["headers"] = request.headers
        captured["body"] = await request.aread()

        class ResultStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'{"run_id":"run-9"}'

        return httpx.Response(
            200,
            stream=ResultStream(),
            headers={"content-type": "application/json"},
        )

    settings = LangGraphGatewaySettings(
        enabled=True,
        upstream_url="http://agent-server",
        upstream_audience="agent-server:fixture",
        deployment_ref="deployment-a",
        upstream_credential_ref="agent.credential",
    )
    transport = HTTPGatewayTransport(
        settings,
        RecordingSecrets(),
        http_transport=httpx.MockTransport(upstream),
    )
    signer = RecordingSigner()

    def route_classifier(method, path):
        order.append("route classification")
        return classify_endpoint(method, path)

    def principal_resolver(request):
        order.append("principal")
        return request.state.principal

    request = governed_request(
        b'{"assistant_id":"assistant-2","input":{"question":"hello"}}',
        receive_hook=lambda: order.append("bounded parse"),
    )
    proxy = GatewayProxy(
        settings=settings,
        transport=transport,
        context_codec=ReservedContextCodec(signer, clock=lambda: 1000),
        policy_guard=AllowPolicy(),
        budget_checker=AllowBudget(),
        classifier=RecordingClassifier(),
        compatibility=supported_compatibility(),
        principal_resolver=principal_resolver,
        route_classifier=route_classifier,
        clock=lambda: 1000,
        correlation_factory=lambda: "corr-1",
    )

    response = await proxy.handle_http(request)
    response_body = b"".join([chunk async for chunk in response.body_iterator])

    assert order == [
        "route classification",
        "principal",
        "bounded parse",
        "admission",
        "signed injection",
        "credential replacement",
        "transport",
    ]
    assert response_body == b'{"run_id":"run-9"}'
    assert response.headers["x-correlation-id"] == "corr-1"
    assert response.headers["x-zeroth-governance-level"] == "admission"
    assert captured["headers"]["authorization"] == "Bearer upstream-secret"
    forwarded = json.loads(captured["body"])
    token = forwarded["config"]["configurable"]["_zeroth"]
    claims = ReservedContextCodec(signer, clock=lambda: 1000).decode(
        token,
        audience="agent-server:fixture",
        deployment_ref="deployment-a",
    )
    assert claims.model_dump() == {
        "schema_version": 1,
        "tenant_id": "tenant-a",
        "principal_id": "user-7",
        "roles": ("operator",),
        "deployment_ref": "deployment-a",
        "audience": "agent-server:fixture",
        "correlation_id": "corr-1",
        "policy_version": "sha256:policy",
        "issued_at": 1000,
        "expires_at": 1300,
        "content_classification": "internal",
    }
    await transport.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "body", "policy", "signer", "max_bytes", "compatibility", "status", "code"),
    [
        (
            "/threads/thread-4/runs",
            b'{"input":{}}',
            DenyPolicy(),
            EnvHmacSigner(key_id="k", keys={"k": b"key"}),
            1024,
            supported_compatibility(),
            403,
            "zeroth.policy_denied",
        ),
        (
            "/threads/thread-4/runs",
            b"not-json",
            AllowPolicy(),
            EnvHmacSigner(key_id="k", keys={"k": b"key"}),
            1024,
            supported_compatibility(),
            400,
            "zeroth.invalid_request",
        ),
        (
            "/threads/thread-4/runs",
            b'{"input":"too-large"}',
            AllowPolicy(),
            EnvHmacSigner(key_id="k", keys={"k": b"key"}),
            4,
            supported_compatibility(),
            413,
            "zeroth.request_too_large",
        ),
        (
            "/crons",
            b"{}",
            AllowPolicy(),
            EnvHmacSigner(key_id="k", keys={"k": b"key"}),
            1024,
            supported_compatibility(),
            501,
            "zeroth.unsupported_endpoint",
        ),
        (
            "/threads/thread-4/runs",
            b'{"input":{}}',
            AllowPolicy(),
            NullSigner(),
            1024,
            supported_compatibility(),
            503,
            "zeroth.context_signing_unavailable",
        ),
        (
            "/threads/thread-4/runs",
            b'{"input":{}}',
            AllowPolicy(),
            EnvHmacSigner(key_id="k", keys={"k": b"key"}),
            1024,
            CompatibilityResult(
                tested_langgraph_versions=("1.2.9",),
                tested_agent_server_versions=("0.11.1",),
                status=CompatibilityStatus.UNSUPPORTED,
            ),
            501,
            "zeroth.unsupported_upstream",
        ),
    ],
)
async def test_preflight_denials_use_stable_safe_errors_and_never_connect(
    path, body, policy, signer, max_bytes, compatibility, status, code
):
    calls = 0

    async def upstream(request):
        nonlocal calls
        calls += 1
        return httpx.Response(204)

    settings = LangGraphGatewaySettings(
        enabled=True,
        upstream_url="http://agent-server",
        upstream_audience="fixture",
        deployment_ref="deployment-a",
        max_governed_body_bytes=max_bytes,
    )
    transport = HTTPGatewayTransport(
        settings,
        EnvSecretProvider(),
        http_transport=httpx.MockTransport(upstream),
    )
    proxy = GatewayProxy(
        settings=settings,
        transport=transport,
        context_codec=ReservedContextCodec(signer),
        policy_guard=policy,
        budget_checker=AllowBudget(),
        compatibility=compatibility,
        correlation_factory=lambda: "corr-safe",
    )

    response = await proxy.handle_http(governed_request(body, path=path))

    assert response.status_code == status
    error = json.loads(response.body)
    assert error["code"] == code
    assert error["correlation_id"] == "corr-safe"
    assert error["retryable"] is (code == "zeroth.context_signing_unavailable")
    assert isinstance(error["reason"], str) and error["reason"]
    assert b"too-large" not in response.body
    assert calls == 0
    await transport.aclose()


class RecordingEventSink:
    def __init__(self, *, fail=False):
        self.events = []
        self.fail = fail

    async def emit(self, event):
        self.events.append(event)
        if self.fail:
            raise RuntimeError("audit backend unavailable")


@pytest.mark.asyncio
async def test_unknown_pass_ungoverned_is_marked_and_audited_without_body_rewrite():
    received = {}
    sink = RecordingEventSink()

    async def upstream(request):
        received["body"] = await request.aread()

        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"upstream-bytes"

        return httpx.Response(418, stream=Body(), headers={"content-type": "text/plain"})

    settings = LangGraphGatewaySettings(
        enabled=True,
        upstream_url="http://agent-server",
        upstream_audience="fixture",
        deployment_ref="deployment-a",
        unknown_endpoint_mode="pass_ungoverned",
    )
    transport = HTTPGatewayTransport(
        settings,
        EnvSecretProvider(),
        http_transport=httpx.MockTransport(upstream),
    )
    proxy = GatewayProxy(
        settings=settings,
        transport=transport,
        context_codec=ReservedContextCodec(EnvHmacSigner(key_id="k", keys={"k": b"key"})),
        policy_guard=AllowPolicy(),
        budget_checker=AllowBudget(),
        compatibility=supported_compatibility(),
        event_sink=sink,
        correlation_factory=lambda: "corr-unknown",
    )

    response = await proxy.handle_http(governed_request(b"opaque-body", path="/custom"))
    body = b"".join([chunk async for chunk in response.body_iterator])

    assert response.status_code == 418
    assert body == b"upstream-bytes"
    assert received["body"] == b"opaque-body"
    assert response.headers["x-zeroth-governance"] == "ungoverned"
    assert response.headers["x-correlation-id"] == "corr-unknown"
    assert sink.events[-1].status.value == "upstream_error"
    assert sink.events[-1].disposition.value == "unsupported"
    await transport.aclose()


@pytest.mark.asyncio
async def test_event_sink_failure_is_counted_and_does_not_change_or_reorder_response():
    sink = RecordingEventSink(fail=True)

    async def upstream(request):
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"one-"
                yield b"two"

        return httpx.Response(200, stream=Body(), headers={"content-type": "text/plain"})

    settings = LangGraphGatewaySettings(
        enabled=True,
        upstream_url="http://agent-server",
        upstream_audience="fixture",
        deployment_ref="deployment-a",
    )
    transport = HTTPGatewayTransport(
        settings,
        EnvSecretProvider(),
        http_transport=httpx.MockTransport(upstream),
    )
    proxy = GatewayProxy(
        settings=settings,
        transport=transport,
        context_codec=ReservedContextCodec(EnvHmacSigner(key_id="k", keys={"k": b"key"})),
        policy_guard=AllowPolicy(),
        budget_checker=AllowBudget(),
        compatibility=supported_compatibility(),
        event_sink=sink,
    )

    response = await proxy.handle_http(authenticated_empty_request("/ok"))
    chunks = [chunk async for chunk in response.body_iterator]

    assert chunks == [b"one-", b"two"]
    assert proxy.sink_failure_count == 1
    assert sink.events[-1].status.value == "success"
    await transport.aclose()


@pytest.mark.asyncio
async def test_generator_close_emits_client_disconnect_terminal_event():
    sink = RecordingEventSink()

    async def upstream(request):
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"first"
                yield b"second"

        return httpx.Response(200, stream=Body(), headers={"content-type": "text/plain"})

    settings = LangGraphGatewaySettings(
        enabled=True,
        upstream_url="http://agent-server",
        upstream_audience="fixture",
        deployment_ref="deployment-a",
    )
    transport = HTTPGatewayTransport(
        settings,
        EnvSecretProvider(),
        http_transport=httpx.MockTransport(upstream),
    )
    proxy = GatewayProxy(
        settings=settings,
        transport=transport,
        context_codec=ReservedContextCodec(EnvHmacSigner(key_id="k", keys={"k": b"key"})),
        policy_guard=AllowPolicy(),
        budget_checker=AllowBudget(),
        compatibility=supported_compatibility(),
        event_sink=sink,
    )

    response = await proxy.handle_http(authenticated_empty_request("/ok"))
    assert await anext(response.body_iterator) == b"first"
    await response.body_iterator.aclose()

    assert sink.events[-1].status.value == "client_disconnect"
    await transport.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("upstream_error", "status", "code"),
    [
        (httpx.ConnectError("refused"), 502, "zeroth.upstream_unavailable"),
        (
            httpx.ConnectTimeout("TOP-SECRET-timeout-value"),
            504,
            "zeroth.upstream_timeout",
        ),
    ],
)
async def test_transport_failures_map_to_safe_gateway_errors(upstream_error, status, code):
    async def upstream(request):
        raise upstream_error

    settings = LangGraphGatewaySettings(
        enabled=True,
        upstream_url="http://agent-server",
        upstream_audience="fixture",
        deployment_ref="deployment-a",
    )
    transport = HTTPGatewayTransport(
        settings,
        EnvSecretProvider(),
        http_transport=httpx.MockTransport(upstream),
    )
    proxy = GatewayProxy(
        settings=settings,
        transport=transport,
        context_codec=ReservedContextCodec(EnvHmacSigner(key_id="k", keys={"k": b"key"})),
        policy_guard=AllowPolicy(),
        budget_checker=AllowBudget(),
        compatibility=supported_compatibility(),
        correlation_factory=lambda: "corr-transport",
    )

    response = await proxy.handle_http(authenticated_empty_request("/ok"))

    assert response.status_code == status
    assert json.loads(response.body)["code"] == code
    assert b"refused" not in response.body
    assert b"TOP-SECRET-timeout-value" not in response.body
    await transport.aclose()


@pytest.mark.asyncio
async def test_missing_upstream_credential_maps_to_503_without_connecting():
    calls = 0

    async def upstream(request):
        nonlocal calls
        calls += 1
        return httpx.Response(204)

    settings = LangGraphGatewaySettings(
        enabled=True,
        upstream_url="http://agent-server",
        upstream_audience="fixture",
        deployment_ref="deployment-a",
        upstream_credential_ref="private.secret.ref",
    )
    transport = HTTPGatewayTransport(
        settings,
        EnvSecretProvider(),
        http_transport=httpx.MockTransport(upstream),
    )
    proxy = GatewayProxy(
        settings=settings,
        transport=transport,
        context_codec=ReservedContextCodec(EnvHmacSigner(key_id="k", keys={"k": b"key"})),
        policy_guard=AllowPolicy(),
        budget_checker=AllowBudget(),
        compatibility=supported_compatibility(),
        correlation_factory=lambda: "corr-credential",
    )

    response = await proxy.handle_http(authenticated_empty_request("/ok"))

    assert response.status_code == 503
    assert json.loads(response.body)["code"] == "zeroth.upstream_credential_unavailable"
    assert b"private.secret.ref" not in response.body
    assert calls == 0
    await transport.aclose()


@pytest.mark.asyncio
async def test_cancellation_during_admission_emits_terminal_event_without_connecting():
    classifier_started = asyncio.Event()
    sink = RecordingEventSink()
    calls = 0

    class BlockingClassifier:
        async def classify(self, payload):
            classifier_started.set()
            await asyncio.Future()

    async def upstream(request):
        nonlocal calls
        calls += 1
        return httpx.Response(204)

    settings = LangGraphGatewaySettings(
        enabled=True,
        upstream_url="http://agent-server",
        upstream_audience="fixture",
        deployment_ref="deployment-a",
    )
    transport = HTTPGatewayTransport(
        settings,
        EnvSecretProvider(),
        http_transport=httpx.MockTransport(upstream),
    )
    proxy = GatewayProxy(
        settings=settings,
        transport=transport,
        context_codec=ReservedContextCodec(EnvHmacSigner(key_id="k", keys={"k": b"key"})),
        policy_guard=AllowPolicy(),
        budget_checker=AllowBudget(),
        classifier=BlockingClassifier(),
        compatibility=supported_compatibility(),
        event_sink=sink,
    )
    raw_body = (
        b'{"assistant_id":"assistant-cancel","run_id":"run-cancel",'
        b'"input":{"secret":"raw-cancel-input"}}'
    )
    task = asyncio.create_task(proxy.handle_http(governed_request(raw_body)))
    await classifier_started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls == 0
    event = sink.events[-1]
    assert event.status.value == "cancellation"
    assert event.correlation.thread_id == "thread-4"
    assert event.correlation.assistant_id == "assistant-cancel"
    assert event.correlation.run_id == "run-cancel"
    assert event.input_sha256 == hashlib.sha256(raw_body).hexdigest()
    assert event.input_size_bytes == len(raw_body)
    assert "raw-cancel-input" not in event.model_dump_json()
    await transport.aclose()


@pytest.mark.asyncio
async def test_known_transparent_protocol_command_preserves_consumed_body_bytes():
    captured_body = None

    async def upstream(request):
        nonlocal captured_body
        captured_body = await request.aread()

        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'{"ok":true}'

        return httpx.Response(200, stream=Body(), headers={"content-type": "application/json"})

    settings = LangGraphGatewaySettings(
        enabled=True,
        upstream_url="http://agent-server",
        upstream_audience="fixture",
        deployment_ref="deployment-a",
    )
    transport = HTTPGatewayTransport(
        settings,
        EnvSecretProvider(),
        http_transport=httpx.MockTransport(upstream),
    )
    proxy = GatewayProxy(
        settings=settings,
        transport=transport,
        context_codec=ReservedContextCodec(EnvHmacSigner(key_id="k", keys={"k": b"key"})),
        policy_guard=AllowPolicy(),
        budget_checker=AllowBudget(),
        compatibility=supported_compatibility(),
    )
    command = b'{"jsonrpc":"2.0","method":"agent.getTree","params":{"depth":3}}'

    response = await proxy.handle_http(governed_request(command, path="/threads/thread-4/commands"))
    response_body = b"".join([chunk async for chunk in response.body_iterator])

    assert response.status_code == 200
    assert response_body == b'{"ok":true}'
    assert captured_body == command
    assert "x-zeroth-governance-level" not in response.headers
    await transport.aclose()


@pytest.mark.asyncio
async def test_governed_terminal_event_keeps_request_ids_when_response_has_none():
    sink = RecordingEventSink()

    async def upstream(request):
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"accepted"

        return httpx.Response(202, stream=Body(), headers={"content-type": "text/plain"})

    settings = LangGraphGatewaySettings(
        enabled=True,
        upstream_url="http://agent-server",
        upstream_audience="fixture",
        deployment_ref="deployment-a",
    )
    transport = HTTPGatewayTransport(
        settings,
        EnvSecretProvider(),
        http_transport=httpx.MockTransport(upstream),
    )
    proxy = GatewayProxy(
        settings=settings,
        transport=transport,
        context_codec=ReservedContextCodec(EnvHmacSigner(key_id="k", keys={"k": b"key"})),
        policy_guard=AllowPolicy(),
        budget_checker=AllowBudget(),
        compatibility=supported_compatibility(),
        event_sink=sink,
    )

    response = await proxy.handle_http(
        governed_request(b'{"assistant_id":"assistant-2","input":{}}')
    )
    _ = [chunk async for chunk in response.body_iterator]

    event = sink.events[-1]
    assert event.correlation.thread_id == "thread-4"
    assert event.correlation.assistant_id == "assistant-2"
    await transport.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_kind", "expected_code", "expected_status", "expected_connects"),
    [
        ("policy", "zeroth.policy_denied", 403, 0),
        ("credential", "zeroth.upstream_credential_unavailable", 503, 0),
        ("connect", "zeroth.upstream_unavailable", 502, 1),
        ("timeout", "zeroth.upstream_timeout", 504, 1),
        ("signer", "zeroth.context_signing_unavailable", 503, 0),
        ("misconfiguration", "zeroth.gateway_misconfigured", 503, 0),
    ],
)
async def test_governed_terminal_failures_keep_known_safe_request_metadata(
    failure_kind, expected_code, expected_status, expected_connects
):
    raw_body = (
        b'{"assistant_id":"assistant-2","run_id":"run-known",'
        b'"input":{"secret":"raw-input-must-not-leak"}}'
    )
    sink = RecordingEventSink()
    connection_count = 0

    async def upstream(request):
        nonlocal connection_count
        connection_count += 1
        if failure_kind == "connect":
            raise httpx.ConnectError("raw-connect-detail")
        if failure_kind == "timeout":
            raise httpx.ConnectTimeout("raw-timeout-detail")
        return httpx.Response(204)

    settings = LangGraphGatewaySettings(
        enabled=True,
        upstream_url="http://agent-server",
        upstream_audience="fixture",
        deployment_ref="deployment-a",
        upstream_credential_ref=(
            "private.raw-secret-ref" if failure_kind == "credential" else None
        ),
    )
    signer = (
        NullSigner() if failure_kind == "signer" else EnvHmacSigner(key_id="k", keys={"k": b"key"})
    )

    def clock():
        if failure_kind == "misconfiguration":
            raise RuntimeError("raw-misconfiguration-detail")
        return 1000

    transport = HTTPGatewayTransport(
        settings,
        EnvSecretProvider(),
        http_transport=httpx.MockTransport(upstream),
    )
    proxy = GatewayProxy(
        settings=settings,
        transport=transport,
        context_codec=ReservedContextCodec(signer, clock=lambda: 1000),
        policy_guard=DenyPolicy() if failure_kind == "policy" else AllowPolicy(),
        budget_checker=AllowBudget(),
        compatibility=supported_compatibility(),
        event_sink=sink,
        clock=clock,
        correlation_factory=lambda: "corr-failure",
    )

    response = await proxy.handle_http(governed_request(raw_body))

    assert response.status_code == expected_status
    envelope = json.loads(response.body)
    assert envelope["code"] == expected_code
    assert envelope["correlation_id"] == "corr-failure"
    assert "raw-input-must-not-leak" not in response.body.decode()
    assert "raw-connect-detail" not in response.body.decode()
    assert "raw-timeout-detail" not in response.body.decode()
    assert "raw-misconfiguration-detail" not in response.body.decode()
    assert "private.raw-secret-ref" not in response.body.decode()
    assert connection_count == expected_connects

    [event] = sink.events
    assert event.correlation.thread_id == "thread-4"
    assert event.correlation.assistant_id == "assistant-2"
    assert event.correlation.run_id == "run-known"
    assert event.input_sha256 == hashlib.sha256(raw_body).hexdigest()
    assert event.input_size_bytes == len(raw_body)
    assert "raw-input-must-not-leak" not in event.model_dump_json()
    await transport.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "max_body_bytes", "expected_code"),
    [
        (b'{"assistant_id":"incomplete"', 1024, "zeroth.invalid_request"),
        (b'{"assistant_id":"not-safely-known"}', 4, "zeroth.request_too_large"),
    ],
)
async def test_unvalidated_or_incomplete_body_does_not_create_input_fingerprint(
    body, max_body_bytes, expected_code
):
    sink = RecordingEventSink()
    calls = 0

    async def upstream(request):
        nonlocal calls
        calls += 1
        return httpx.Response(204)

    settings = LangGraphGatewaySettings(
        enabled=True,
        upstream_url="http://agent-server",
        upstream_audience="fixture",
        deployment_ref="deployment-a",
        max_governed_body_bytes=max_body_bytes,
    )
    transport = HTTPGatewayTransport(
        settings,
        EnvSecretProvider(),
        http_transport=httpx.MockTransport(upstream),
    )
    proxy = GatewayProxy(
        settings=settings,
        transport=transport,
        context_codec=ReservedContextCodec(EnvHmacSigner(key_id="k", keys={"k": b"key"})),
        policy_guard=AllowPolicy(),
        budget_checker=AllowBudget(),
        compatibility=supported_compatibility(),
        event_sink=sink,
    )

    response = await proxy.handle_http(governed_request(body))

    assert json.loads(response.body)["code"] == expected_code
    assert calls == 0
    [event] = sink.events
    assert event.correlation.thread_id == "thread-4"
    assert event.correlation.assistant_id is None
    assert event.correlation.run_id is None
    assert event.input_sha256 is None
    assert event.input_size_bytes is None
    assert "incomplete" not in event.model_dump_json()
    assert "not-safely-known" not in event.model_dump_json()
    await transport.aclose()
