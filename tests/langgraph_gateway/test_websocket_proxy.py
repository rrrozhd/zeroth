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
from zeroth.core.langgraph_gateway.headers import UpstreamCredentialUnavailableError
from zeroth.core.langgraph_gateway.routes import (
    GatewayWebSocketEndpoint,
    WebSocketGatewayCloseError,
    WebSocketGatewayHandler,
    register_gateway_routes,
)
from zeroth.core.langgraph_gateway.transport import (
    HTTPGatewayTransport,
    WebSocketClientError,
)
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
        send_error: Exception | None = None,
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
        self._send_error = send_error
        self.accepted = False
        self.accepted_subprotocol: str | None = None
        self.accepted_headers: list[tuple[bytes, bytes]] = []
        self.closed: tuple[int, str] | None = None
        self.close_calls: list[tuple[int, str]] = []
        self.accepted_event = asyncio.Event()
        self.receive_cancelled = asyncio.Event()

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
        try:
            return await self._incoming.get()
        except asyncio.CancelledError:
            self.receive_cancelled.set()
            raise

    async def send_text(self, data: str) -> None:
        if self._send_error is not None:
            raise self._send_error
        if self._slow_send is not None:
            await self._slow_send.wait()
        await self._outgoing.put(("text", data))

    async def send_bytes(self, data: bytes) -> None:
        if self._send_error is not None:
            raise self._send_error
        if self._slow_send is not None:
            await self._slow_send.wait()
        await self._outgoing.put(("bytes", data))

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed = (code, reason or "")
        self.close_calls.append(self.closed)
        await self._outgoing.put(("close", self.closed))

    async def client_text(self, data: str) -> None:
        await self._incoming.put({"type": "websocket.receive", "text": data})

    async def client_bytes(self, data: bytes) -> None:
        await self._incoming.put({"type": "websocket.receive", "bytes": data})

    async def client_close(self, code: int = 1000, reason: str = "") -> None:
        await self._incoming.put({"type": "websocket.disconnect", "code": code, "reason": reason})

    async def next_server_event(self) -> tuple[str, Any]:
        return await asyncio.wait_for(self._outgoing.get(), timeout=2)


def settings(upstream_url: str, **updates: Any) -> LangGraphGatewaySettings:
    values = dict(
        enabled=True,
        upstream_url=upstream_url,
        upstream_audience="agent-server:fixture",
        deployment_ref="deployment-a",
    )
    values.update(updates)
    return LangGraphGatewaySettings(**values)


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


@pytest.mark.asyncio
async def test_slow_consumer_total_queued_bytes_stays_bounded_without_reorder():
    release = asyncio.Event()
    frames = [f"{index:02d}-" + ("x" * 13) for index in range(6)]

    async def upstream(connection) -> None:
        for frame in frames:
            await connection.send(frame)
        await connection.close(1000, "done")

    async with websockets.serve(upstream, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        configured = settings(f"http://127.0.0.1:{port}")
        transport = HTTPGatewayTransport(
            configured,
            EnvSecretProvider(),
            websocket_queue_size=16,
            websocket_max_message_bytes=16,
            websocket_max_queued_bytes=20,
        )
        websocket = MemoryWebSocket(slow_send=release)
        task = asyncio.create_task(transport.forward_websocket(websocket, tenant_id="tenant-a"))
        await websocket.accepted_event.wait()
        await asyncio.sleep(0.05)
        assert transport.websocket_max_buffered_bytes <= 20
        release.set()

        received = [await websocket.next_server_event() for _ in range(len(frames) + 1)]
        await asyncio.wait_for(task, timeout=2)
        assert received[:-1] == [("text", frame) for frame in frames]
        assert received[-1] == ("close", (1000, "done"))
        assert transport.websocket_max_buffered_bytes <= 20
        await transport.aclose()


@pytest.mark.asyncio
async def test_oversized_client_frame_closes_once_and_closes_upstream():
    upstream_closed = asyncio.Event()

    async def upstream(connection) -> None:
        try:
            await connection.recv()
        except websockets.ConnectionClosed:
            upstream_closed.set()

    async with websockets.serve(upstream, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        configured = settings(f"http://127.0.0.1:{port}")
        transport = HTTPGatewayTransport(
            configured,
            EnvSecretProvider(),
            websocket_max_message_bytes=32,
            websocket_max_queued_bytes=64,
        )
        active, _, _, _ = handler(f"http://127.0.0.1:{port}", transport=transport)
        websocket = MemoryWebSocket()
        websocket.state.principal = principal()
        websocket.state.correlation_id = "corr-ws"
        task = asyncio.create_task(active.handle(websocket))
        await websocket.accepted_event.wait()
        await websocket.client_text("x" * 33)

        await asyncio.wait_for(task, timeout=2)

        assert websocket.close_calls == [(4400, "zeroth.websocket_message_too_large")]
        await asyncio.wait_for(upstream_closed.wait(), timeout=2)
        await transport.aclose()


@pytest.mark.asyncio
async def test_oversized_upstream_frame_closes_downstream_with_safe_1009():
    async def upstream(connection) -> None:
        await connection.send("x" * 65)
        await connection.wait_closed()

    async with websockets.serve(upstream, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        configured = settings(f"http://127.0.0.1:{port}")
        transport = HTTPGatewayTransport(
            configured,
            EnvSecretProvider(),
            websocket_max_message_bytes=64,
            websocket_max_queued_bytes=128,
        )
        websocket = MemoryWebSocket()

        await transport.forward_websocket(websocket, tenant_id="tenant-a")

        assert websocket.close_calls == [(1009, "websocket message too large")]
        await transport.aclose()


def _bridge_pump_tasks() -> list[asyncio.Task[Any]]:
    current = asyncio.current_task()
    return [
        task
        for task in asyncio.all_tasks()
        if task is not current
        and "HTTPGatewayTransport._bridge_websocket.<locals>" in repr(task.get_coro())
    ]


@pytest.mark.asyncio
async def test_cancelling_forward_websocket_closes_both_sockets_and_all_pumps():
    upstream_closed: asyncio.Future[tuple[int | None, str]] = asyncio.Future()

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

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert websocket.closed == (1001, "gateway stream cancelled")
        assert await asyncio.wait_for(upstream_closed, timeout=2) == (
            1001,
            "gateway stream cancelled",
        )
        assert websocket.receive_cancelled.is_set()
        await asyncio.sleep(0)
        assert _bridge_pump_tasks() == []
        await transport.aclose()


@pytest.mark.asyncio
async def test_downstream_pump_failure_cancels_siblings_and_closes_both_sockets():
    upstream_closed: asyncio.Future[tuple[int | None, str]] = asyncio.Future()

    async def upstream(connection) -> None:
        await connection.send("trigger downstream failure")
        try:
            await connection.recv()
        except websockets.ConnectionClosed:
            upstream_closed.set_result((connection.close_code, connection.close_reason or ""))

    async with websockets.serve(upstream, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        configured = settings(f"http://127.0.0.1:{port}")
        transport = HTTPGatewayTransport(configured, EnvSecretProvider())
        websocket = MemoryWebSocket(send_error=RuntimeError("downstream send failed"))

        with pytest.raises(ExceptionGroup) as caught:
            await transport.forward_websocket(websocket, tenant_id="tenant-a")

        assert any(
            isinstance(error, RuntimeError) and str(error) == "downstream send failed"
            for error in caught.value.exceptions
        )
        assert websocket.closed == (1011, "gateway stream failed")
        assert await asyncio.wait_for(upstream_closed, timeout=2) == (
            1011,
            "gateway stream failed",
        )
        assert websocket.receive_cancelled.is_set()
        await asyncio.sleep(0)
        assert _bridge_pump_tasks() == []
        await transport.aclose()


@pytest.mark.asyncio
async def test_governance_error_closes_downstream_once_while_closing_upstream():
    upstream_closed = asyncio.Event()

    async def upstream(connection) -> None:
        try:
            await connection.recv()
        except websockets.ConnectionClosed:
            upstream_closed.set()

    async with websockets.serve(upstream, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        active, _, _, transport = handler(f"http://127.0.0.1:{port}")
        websocket = MemoryWebSocket()
        websocket.state.principal = principal()
        websocket.state.correlation_id = "corr-ws"
        task = asyncio.create_task(active.handle(websocket))
        await websocket.accepted_event.wait()
        await websocket.client_text('{"method":"run.start","params":[]}')

        await asyncio.wait_for(task, timeout=2)

        assert websocket.close_calls == [(4400, "zeroth.invalid_request")]
        await asyncio.wait_for(upstream_closed.wait(), timeout=2)
        await transport.aclose()


@pytest.mark.asyncio
async def test_aclose_cancels_and_drains_active_websocket_then_rejects_new_dial():
    connection_count = 0
    upstream_closed = asyncio.Event()

    async def upstream(connection) -> None:
        nonlocal connection_count
        connection_count += 1
        try:
            await connection.recv()
        except websockets.ConnectionClosed:
            upstream_closed.set()

    async with websockets.serve(upstream, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        configured = settings(f"http://127.0.0.1:{port}")
        transport = HTTPGatewayTransport(configured, EnvSecretProvider())
        websocket = MemoryWebSocket()
        forward = asyncio.create_task(transport.forward_websocket(websocket, tenant_id="tenant-a"))
        await websocket.accepted_event.wait()

        await asyncio.wait_for(transport.aclose(), timeout=2)

        assert forward.cancelled()
        assert websocket.closed == (1001, "gateway stream cancelled")
        await asyncio.wait_for(upstream_closed.wait(), timeout=2)
        assert transport.websocket_active_count == 0
        with pytest.raises(RuntimeError, match="transport is closed"):
            await transport.forward_websocket(MemoryWebSocket(), tenant_id="tenant-a")
        assert connection_count == 1


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


def handler(upstream_url: str, *, transport=None, settings_overrides=None):
    configured = settings(upstream_url, **(settings_overrides or {}))
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "frame",
    [
        '{"method":"run\\u002estart","params":{},"padding":"xxxxxxxx"}',
        '{"metho\\u0064":"run.start","params":{},"padding":"xxxxxxxx"}',
        '{"method":"subscription.subscribe","padding":"xxxxxxxxxxxxxxxx"}',
    ],
)
async def test_oversized_text_frames_fail_closed_before_json_classification(frame: str):
    active, _, _, transport = handler(
        "http://127.0.0.1:9",
        settings_overrides={"max_governed_body_bytes": 32},
    )
    websocket = MemoryWebSocket()
    websocket.state.principal = principal()
    websocket.state.correlation_id = "corr-ws"

    with pytest.raises(WebSocketGatewayCloseError) as caught:
        await active.transform_client_message(websocket, frame)

    assert (caught.value.code, caught.value.reason) == (
        4400,
        "zeroth.request_too_large",
    )
    await transport.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "frame",
    [
        '{"method":"subscription.subscribe","method":"run.start","params":{}}',
        '{"method":"subscription.subscribe","metho\\u0064":"run.start","params":{}}',
        '{"method":"run.start","params":{},"params":{"input":"spoof"}}',
    ],
)
async def test_duplicate_json_keys_are_rejected_as_ambiguous(frame: str):
    active, _, _, transport = handler("http://127.0.0.1:9")
    websocket = MemoryWebSocket()
    websocket.state.principal = principal()
    websocket.state.correlation_id = "corr-ws"

    with pytest.raises(WebSocketGatewayCloseError) as caught:
        await active.transform_client_message(websocket, frame)

    assert (caught.value.code, caught.value.reason) == (4400, "zeroth.invalid_request")
    await transport.aclose()


class RecordingWebSocketHandler:
    def __init__(self) -> None:
        self.calls = 0

    async def handle(self, websocket) -> None:
        self.calls += 1
        assert websocket.state.principal.subject == "real-user"
        assert websocket.state.correlation_id == "corr-fixed"


def _authenticator() -> ServiceAuthenticator:
    return ServiceAuthenticator(
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("protocol_header", "correlation_id"),
    [
        (b"", "corr"),
        (b"ok,", "corr"),
        (b"ok, bad protocol", "corr"),
        (b"ok, \x01bad", "corr"),
        (b",".join(f"p{index}".encode() for index in range(17)), "corr"),
        (b"x" * 129, "corr"),
        (b"lg-v2", "corr\nspoof"),
        (b"lg-v2", "x" * 129),
        (b"lg-v2", "corr-\N{SNOWMAN}"),
    ],
)
async def test_invalid_handshake_metadata_is_rejected_before_upstream_dial(
    protocol_header: bytes,
    correlation_id: str,
):
    connection_count = 0

    async def upstream(connection) -> None:
        nonlocal connection_count
        connection_count += 1
        await connection.wait_closed()

    async with websockets.serve(upstream, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        configured = settings(f"http://127.0.0.1:{port}")
        transport = HTTPGatewayTransport(configured, EnvSecretProvider())
        websocket = MemoryWebSocket(headers=[(b"sec-websocket-protocol", protocol_header)])

        with pytest.raises(WebSocketClientError) as caught:
            await transport.forward_websocket(
                websocket,
                tenant_id="tenant-a",
                correlation_id=correlation_id,
            )

        assert (caught.value.code, caught.value.reason) == (
            4400,
            "zeroth.invalid_websocket_handshake",
        )
        assert connection_count == 0
        await transport.aclose()


@pytest.mark.asyncio
async def test_authenticated_invalid_handshake_maps_one_safe_close_without_dial():
    connection_count = 0

    async def upstream(connection) -> None:
        nonlocal connection_count
        connection_count += 1
        await connection.wait_closed()

    async with websockets.serve(upstream, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        configured = settings(f"http://127.0.0.1:{port}")
        transport = HTTPGatewayTransport(configured, EnvSecretProvider())
        active, _, _, _ = handler(f"http://127.0.0.1:{port}", transport=transport)
        endpoint = GatewayWebSocketEndpoint(
            authenticator=_authenticator(),
            handler=active,
            correlation_factory=lambda: "corr-fixed",
        )
        websocket = MemoryWebSocket(
            headers=[
                (b"x-api-key", b"client-key"),
                (b"sec-websocket-protocol", b"bad protocol"),
            ]
        )

        await endpoint(websocket)

        assert websocket.close_calls == [(4400, "zeroth.invalid_websocket_handshake")]
        assert connection_count == 0
        await transport.aclose()


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"websocket_max_message_bytes": 0}, "websocket_max_message_bytes"),
        ({"websocket_max_message_bytes": True}, "websocket_max_message_bytes"),
        ({"websocket_max_queued_bytes": 0}, "websocket_max_queued_bytes"),
        ({"websocket_max_queued_bytes": float("inf")}, "websocket_max_queued_bytes"),
    ],
)
def test_websocket_memory_bounds_require_finite_positive_integers(options, message):
    with pytest.raises(ValueError, match=message):
        HTTPGatewayTransport(
            settings("http://agent-server.invalid"),
            EnvSecretProvider(),
            **options,
        )


@pytest.mark.asyncio
async def test_route_authenticates_before_accept_and_sets_principal_and_correlation():
    downstream = RecordingWebSocketHandler()
    endpoint = GatewayWebSocketEndpoint(
        authenticator=_authenticator(),
        handler=downstream,
        correlation_factory=lambda: "corr-fixed",
    )
    websocket = MemoryWebSocket(headers=[(b"x-api-key", b"client-key")])

    await endpoint(websocket)

    assert downstream.calls == 1
    assert websocket.accepted is False
    assert websocket.state.principal.subject == "real-user"


class FailingHandshakeTransport:
    def __init__(self, failure: Exception, *, accept_first: bool = False) -> None:
        self.failure = failure
        self.accept_first = accept_first
        self.calls = 0

    async def forward_websocket(self, websocket, **kwargs) -> None:
        self.calls += 1
        assert kwargs["tenant_id"] == "tenant-a"
        assert websocket.state.principal.subject == "real-user"
        if self.accept_first:
            await websocket.accept()
        raise self.failure


def _endpoint_with_transport(transport) -> GatewayWebSocketEndpoint:
    active, _, _, _ = handler("http://agent-server.invalid", transport=transport)
    return GatewayWebSocketEndpoint(
        authenticator=_authenticator(),
        handler=active,
        correlation_factory=lambda: "corr-fixed",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_close"),
    [
        (
            UpstreamCredentialUnavailableError(),
            (4503, "zeroth.upstream_credential_unavailable"),
        ),
        (TimeoutError("client-key must not leak"), (4504, "zeroth.upstream_timeout")),
        (
            OSError("Authorization: Bearer client-key must not leak"),
            (4502, "zeroth.upstream_unavailable"),
        ),
    ],
)
async def test_authenticated_gateway_handshake_failures_use_safe_stable_closes(
    failure: Exception,
    expected_close: tuple[int, str],
):
    transport = FailingHandshakeTransport(failure)
    endpoint = _endpoint_with_transport(transport)
    websocket = MemoryWebSocket(
        headers=[
            (b"x-api-key", b"client-key"),
            (b"authorization", b"Bearer client-secret"),
        ]
    )

    await endpoint(websocket)

    assert transport.calls == 1
    assert websocket.accepted is False
    assert websocket.closed == expected_close
    assert "client-key" not in websocket.closed[1]
    assert "client-secret" not in websocket.closed[1]


@pytest.mark.asyncio
async def test_post_accept_stream_failure_is_not_mapped_as_a_handshake_failure():
    transport = FailingHandshakeTransport(
        OSError("runtime stream failure"),
        accept_first=True,
    )
    endpoint = _endpoint_with_transport(transport)
    websocket = MemoryWebSocket(headers=[(b"x-api-key", b"client-key")])

    with pytest.raises(OSError, match="runtime stream failure"):
        await endpoint(websocket)

    assert websocket.accepted is True
    assert websocket.closed is None


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
