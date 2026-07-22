from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
import websockets
from starlette.applications import Starlette

from zeroth.core.config.settings import LangGraphGatewaySettings
from zeroth.core.econ.budget import BudgetCheckResult
from zeroth.core.identity import AuthMethod, AuthenticatedPrincipal, ServiceRole
from zeroth.core.langgraph_gateway.context import ReservedContextCodec
from zeroth.core.langgraph_gateway.routes import (
    GatewayWebSocketEndpoint,
    WebSocketGatewayHandler,
    register_gateway_routes,
)
from zeroth.core.langgraph_gateway.transport import HTTPGatewayTransport
from zeroth.core.policy.models import RunAdmissionResult
from zeroth.core.secrets.provider import EnvSecretProvider
from zeroth.core.service.auth import (
    ServiceAuthConfig,
    ServiceAuthenticator,
    StaticApiKeyCredential,
)
from zeroth.core.signing import EnvHmacSigner


class MemoryWebSocket:
    def __init__(
        self,
        *,
        headers: list[tuple[bytes, bytes]] | None = None,
        path: str = "/threads/thread-a/stream/events",
        query_string: bytes = b"cursor=7",
        slow_send: asyncio.Event | None = None,
    ) -> None:
        self.scope: dict[str, Any] = {
            "type": "websocket",
            "scheme": "ws",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query_string,
            "headers": headers or [],
            "path_params": {"thread_id": "thread-a"},
            "state": {},
        }
        self.state = SimpleNamespace()
        self.path_params = self.scope["path_params"]
        self._incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._outgoing: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self._slow_send = slow_send
        self.accepted = False
        self.accepted_subprotocol: str | None = None
        self.accepted_headers: list[tuple[bytes, bytes]] = []
        self.closed: tuple[int, str] | None = None
        self.accepted_event = asyncio.Event()

    @property
    def headers(self):
        from starlette.datastructures import Headers

        return Headers(raw=self.scope["headers"])

    async def accept(self, subprotocol=None, headers=None) -> None:
        self.accepted = True
        self.accepted_subprotocol = subprotocol
        self.accepted_headers = list(headers or [])
        self.accepted_event.set()

    async def receive(self) -> dict[str, Any]:
        return await self._incoming.get()

    async def send_text(self, data: str) -> None:
        if self._slow_send is not None:
            await self._slow_send.wait()
        await self._outgoing.put(("text", data))

    async def send_bytes(self, data: bytes) -> None:
        if self._slow_send is not None:
            await self._slow_send.wait()
        await self._outgoing.put(("bytes", data))

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed = (code, reason or "")
        await self._outgoing.put(("close", self.closed))

    async def client_text(self, data: str) -> None:
        await self._incoming.put({"type": "websocket.receive", "text": data})

    async def client_bytes(self, data: bytes) -> None:
        await self._incoming.put({"type": "websocket.receive", "bytes": data})

    async def client_close(self, code: int = 1000, reason: str = "") -> None:
        await self._incoming.put({"type": "websocket.disconnect", "code": code, "reason": reason})

    async def next_server_event(self) -> tuple[str, Any]:
        return await asyncio.wait_for(self._outgoing.get(), timeout=2)


def settings(upstream_url: str) -> LangGraphGatewaySettings:
    return LangGraphGatewaySettings(
        enabled=True,
        upstream_url=upstream_url,
        upstream_audience="agent-server:fixture",
        deployment_ref="deployment-a",
    )


@pytest.mark.asyncio
async def test_real_upstream_preserves_subprotocol_text_binary_order_headers_and_ping_pong():
    observed: dict[str, Any] = {}

    async def upstream(connection) -> None:
        observed["path"] = connection.request.path
        observed["headers"] = dict(connection.request.headers)
        observed["subprotocol"] = connection.subprotocol
        pong = await connection.ping(b"liveness")
        await pong
        for _ in range(3):
            message = await connection.recv()
            await connection.send(message)
        await connection.close(4321, "upstream-finished")

    async with websockets.serve(upstream, "127.0.0.1", 0, subprotocols=["lg-v2"]) as server:
        port = server.sockets[0].getsockname()[1]
        configured = settings(f"http://127.0.0.1:{port}/base")
        transport = HTTPGatewayTransport(configured, EnvSecretProvider())
        websocket = MemoryWebSocket(
            headers=[
                (b"sec-websocket-protocol", b"other, lg-v2"),
                (b"authorization", b"Bearer client-secret"),
                (b"x-api-key", b"client-key"),
                (b"x-trace", b"kept"),
            ]
        )
        task = asyncio.create_task(transport.forward_websocket(websocket, tenant_id="tenant-a"))
        await asyncio.wait_for(websocket.accepted_event.wait(), timeout=2)
        await websocket.client_text("one")
        await websocket.client_bytes(b"\x00two")
        await websocket.client_text("three")

        assert [await websocket.next_server_event() for _ in range(4)] == [
            ("text", "one"),
            ("bytes", b"\x00two"),
            ("text", "three"),
            ("close", (4321, "upstream-finished")),
        ]
        await asyncio.wait_for(task, timeout=2)
        assert websocket.accepted_subprotocol == "lg-v2"
        assert observed["subprotocol"] == "lg-v2"
        assert observed["path"] == "/base/threads/thread-a/stream/events?cursor=7"
        assert observed["headers"]["x-trace"] == "kept"
        assert "authorization" not in observed["headers"]
        assert "x-api-key" not in observed["headers"]
        await transport.aclose()


@pytest.mark.asyncio
async def test_client_first_close_code_and_reason_reach_upstream():
    upstream_closed: asyncio.Future[tuple[int, str]] = asyncio.Future()

    async def upstream(connection) -> None:
        try:
            await connection.recv()
        except websockets.ConnectionClosed:
            upstream_closed.set_result((connection.close_code, connection.close_reason or ""))

    async with websockets.serve(upstream, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        configured = settings(f"http://127.0.0.1:{port}")
        transport = HTTPGatewayTransport(configured, EnvSecretProvider())
        websocket = MemoryWebSocket()
        task = asyncio.create_task(transport.forward_websocket(websocket, tenant_id="tenant-a"))
        await websocket.accepted_event.wait()
        await websocket.client_close(4100, "client-finished")

        await asyncio.wait_for(task, timeout=2)
        assert await asyncio.wait_for(upstream_closed, timeout=2) == (4100, "client-finished")
        await transport.aclose()


@pytest.mark.asyncio
async def test_abrupt_upstream_disconnect_is_propagated_as_abnormal_gateway_close():
    async def upstream(connection) -> None:
        connection.transport.abort()

    async with websockets.serve(upstream, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        configured = settings(f"http://127.0.0.1:{port}")
        transport = HTTPGatewayTransport(configured, EnvSecretProvider())
        websocket = MemoryWebSocket()

        await transport.forward_websocket(websocket, tenant_id="tenant-a")

        assert await websocket.next_server_event() == (
            "close",
            (1011, "upstream disconnected abnormally"),
        )
        await transport.aclose()


@pytest.mark.asyncio
async def test_slow_consumer_uses_configurable_bounded_queue_without_reorder_or_drop():
    release = asyncio.Event()
    frame_count = 40

    async def upstream(connection) -> None:
        for index in range(frame_count):
            await connection.send(str(index))
        await connection.close(1000, "done")

    async with websockets.serve(upstream, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        configured = settings(f"http://127.0.0.1:{port}")
        transport = HTTPGatewayTransport(
            configured,
            EnvSecretProvider(),
            websocket_queue_size=2,
        )
        websocket = MemoryWebSocket(slow_send=release)
        task = asyncio.create_task(transport.forward_websocket(websocket, tenant_id="tenant-a"))
        await websocket.accepted_event.wait()
        await asyncio.sleep(0.05)
        assert transport.websocket_max_buffered_frames <= 2
        release.set()

        frames = [await websocket.next_server_event() for _ in range(frame_count + 1)]
        await asyncio.wait_for(task, timeout=2)
        assert frames[:-1] == [("text", str(index)) for index in range(frame_count)]
        assert frames[-1] == ("close", (1000, "done"))
        assert transport.websocket_max_buffered_frames <= 2
        await transport.aclose()


class AllowPolicy:
    def __init__(self) -> None:
        self.requests = []

    def evaluate_run_admission(self, request):
        self.requests.append(request)
        return RunAdmissionResult(allowed=True, policy_version="sha256:policy")


class AllowBudget:
    async def check_budget_status(self, tenant_id: str) -> BudgetCheckResult:
        return BudgetCheckResult(allowed=True, spend_usd=1.0, cap_usd=10.0)


class InternalClassifier:
    async def classify(self, payload: object) -> str:
        return "internal"


def principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        subject="real-user",
        auth_method=AuthMethod.API_KEY,
        roles=[ServiceRole.OPERATOR],
        tenant_id="tenant-a",
        credential_id="credential-a",
    )


def handler(upstream_url: str, *, transport=None):
    configured = settings(upstream_url)
    policy = AllowPolicy()
    active_transport = transport or HTTPGatewayTransport(configured, EnvSecretProvider())
    codec = ReservedContextCodec(
        EnvHmacSigner(key_id="context", keys={"context": b"signing-secret"}),
        clock=lambda: 100,
        max_ttl_seconds=300,
    )
    return (
        WebSocketGatewayHandler(
            settings=configured,
            transport=active_transport,
            context_codec=codec,
            policy_guard=policy,
            budget_checker=AllowBudget(),
            classifier=InternalClassifier(),
            clock=lambda: 100,
        ),
        policy,
        codec,
        active_transport,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["run.start", "input.respond"])
async def test_governed_in_band_commands_admit_and_replace_spoofed_identity(method: str):
    active, policy, codec, transport = handler("http://127.0.0.1:9")
    websocket = MemoryWebSocket()
    websocket.state.principal = principal()
    websocket.state.correlation_id = "corr-ws"
    payload = {
        "id": 1,
        "method": method,
        "principal_id": "attacker",
        "params": {
            "assistant_id": "assistant-a",
            "input": {"message": "hello"},
            "metadata": {"_zeroth": "forged"},
            "config": {"configurable": {"_zeroth": "forged"}},
        },
    }

    transformed = await active.transform_client_message(websocket, json.dumps(payload))
    result = json.loads(transformed)
    token = result["params"]["config"]["configurable"]["_zeroth"]
    claims = codec.decode(
        token,
        audience="agent-server:fixture",
        deployment_ref="deployment-a",
    )

    assert claims.principal_id == "real-user"
    assert claims.tenant_id == "tenant-a"
    assert claims.correlation_id == "corr-ws"
    assert claims.content_classification == "internal"
    assert policy.requests[0].principal_id == "real-user"
    assert policy.requests[0].thread_id == "thread-a"
    await transport.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "frame",
    [
        b"\x00opaque",
        '{ "id": 7, "method": "subscription.subscribe", "params": {"x": 1} }',
        '{"id":8,"method":"future.extension","identity":"attacker"}',
        "not-json",
    ],
)
async def test_non_run_frames_are_byte_identical(frame: str | bytes):
    active, _, _, transport = handler("http://127.0.0.1:9")
    websocket = MemoryWebSocket()
    websocket.state.principal = principal()
    websocket.state.correlation_id = "corr-ws"

    assert await active.transform_client_message(websocket, frame) == frame
    await transport.aclose()


class RecordingWebSocketHandler:
    def __init__(self) -> None:
        self.calls = 0

    async def handle(self, websocket) -> None:
        self.calls += 1
        assert websocket.state.principal.subject == "real-user"
        assert websocket.state.correlation_id == "corr-fixed"


@pytest.mark.asyncio
async def test_route_authenticates_before_accept_and_sets_principal_and_correlation():
    authenticator = ServiceAuthenticator(
        ServiceAuthConfig(
            api_keys=[
                StaticApiKeyCredential(
                    credential_id="credential-a",
                    secret="client-key",
                    subject="real-user",
                    roles=[ServiceRole.OPERATOR],
                    tenant_id="tenant-a",
                )
            ]
        )
    )
    downstream = RecordingWebSocketHandler()
    endpoint = GatewayWebSocketEndpoint(
        authenticator=authenticator,
        handler=downstream,
        correlation_factory=lambda: "corr-fixed",
    )
    websocket = MemoryWebSocket(headers=[(b"x-api-key", b"client-key")])

    await endpoint(websocket)

    assert downstream.calls == 1
    assert websocket.accepted is False
    assert websocket.state.principal.subject == "real-user"


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [[], [(b"x-api-key", b"wrong")]])
async def test_missing_or_invalid_auth_closes_4401_without_calling_upstream(headers):
    downstream = RecordingWebSocketHandler()
    endpoint = GatewayWebSocketEndpoint(
        authenticator=ServiceAuthenticator(ServiceAuthConfig()),
        handler=downstream,
        correlation_factory=lambda: "corr-fixed",
    )
    websocket = MemoryWebSocket(headers=headers)

    await endpoint(websocket)

    assert downstream.calls == 0
    assert websocket.accepted is False
    assert websocket.closed == (4401, "zeroth.authentication_required")


def test_route_registration_adds_http_catchall_and_exact_protocol_websocket():
    class HTTPProxy:
        async def handle_http(self, request):  # pragma: no cover - registration only
            raise AssertionError

    app = Starlette()
    active, _, _, transport = handler("http://127.0.0.1:9")
    register_gateway_routes(
        app,
        proxy=HTTPProxy(),
        websocket_handler=active,
        authenticator=ServiceAuthenticator(ServiceAuthConfig()),
    )

    assert any(route.path == "/{path:path}" for route in app.routes)
    assert any(route.path == "/threads/{thread_id}/stream/events" for route in app.routes)
    asyncio.run(transport.aclose())
