"""Route registration and governed protocol-v2 WebSocket handling."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from fastapi import APIRouter, FastAPI, WebSocket
from pydantic import ValidationError

from zeroth.core.config.settings import LangGraphGatewaySettings
from zeroth.core.identity import AuthenticatedPrincipal
from zeroth.core.langgraph_gateway.admission import (
    BudgetChecker,
    InputClassifier,
    PolicyAdmissionChecker,
    UnclassifiedInputClassifier,
    admit,
)
from zeroth.core.langgraph_gateway.context import (
    GatewayContextError,
    ReservedContextClaims,
    ReservedContextCodec,
    inject_reserved_context,
)
from zeroth.core.langgraph_gateway.headers import UpstreamCredentialUnavailableError
from zeroth.core.langgraph_gateway.inventory import classify_protocol_command
from zeroth.core.langgraph_gateway.models import AdmissionRequest, RouteDisposition
from zeroth.core.langgraph_gateway.transport import HTTPGatewayTransport, WebSocketMessage
from zeroth.core.observability.correlation import set_correlation_id
from zeroth.core.service.auth import AuthenticationError, ServiceAuthenticator

_CORRELATION_HEADER = "X-Correlation-ID"
_AUTHENTICATION_CLOSE_CODE = 4401
_GOVERNED_METHOD = re.compile(r'"method"\s*:\s*"(?:run\.start|input\.respond)"')


class HTTPGatewayProxy(Protocol):
    async def handle_http(self, request: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class WebSocketGatewayCloseError(Exception):
    """Stable gateway-owned WebSocket close without sensitive values."""

    code: int
    reason: str


class WebSocketGatewayHandler:
    """Apply admission and signed context to in-band run commands."""

    def __init__(
        self,
        *,
        settings: LangGraphGatewaySettings,
        transport: HTTPGatewayTransport,
        context_codec: ReservedContextCodec | None,
        policy_guard: PolicyAdmissionChecker,
        budget_checker: BudgetChecker,
        classifier: InputClassifier | None = None,
        clock: Callable[[], int | float] = time.time,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._context_codec = context_codec
        self._policy_guard = policy_guard
        self._budget_checker = budget_checker
        self._classifier = classifier or UnclassifiedInputClassifier()
        self._clock = clock

    async def handle(self, websocket: WebSocket) -> None:
        """Resolve the upstream handshake and run the structured duplex bridge."""
        principal = self._principal(websocket)
        correlation_id = self._correlation_id(websocket)
        try:
            await self._transport.forward_websocket(
                websocket,
                tenant_id=principal.tenant_id,
                correlation_id=correlation_id,
                transform_client_message=lambda message: self.transform_client_message(
                    websocket, message
                ),
            )
        except BaseExceptionGroup as group:
            close = _find_gateway_close(group)
            if close is None:
                await websocket.close(code=1011, reason="gateway stream failed")
                return
            await websocket.close(code=close.code, reason=close.reason)
        except WebSocketGatewayCloseError as exc:
            await websocket.close(code=exc.code, reason=exc.reason)
        except GatewayContextError as exc:
            await websocket.close(code=_context_close_code(exc), reason=exc.code)
        except UpstreamCredentialUnavailableError:
            if _websocket_was_accepted(websocket):
                raise
            await websocket.close(
                code=4503,
                reason="zeroth.upstream_credential_unavailable",
            )
        except TimeoutError:
            if _websocket_was_accepted(websocket):
                raise
            await websocket.close(code=4504, reason="zeroth.upstream_timeout")
        except Exception as exc:
            if not _websocket_was_accepted(websocket) and _is_upstream_handshake_failure(exc):
                await websocket.close(code=4502, reason="zeroth.upstream_unavailable")
                return
            raise

    async def transform_client_message(
        self,
        websocket: WebSocket,
        message: WebSocketMessage,
    ) -> WebSocketMessage:
        """Govern run commands while leaving every other frame byte-identical."""
        if isinstance(message, bytes):
            return message
        raw = message.encode("utf-8")
        if len(raw) > self._settings.max_governed_body_bytes:
            # Large opaque/non-run frames remain transparent. A cheap method check
            # avoids parsing unbounded JSON merely to discover that fact.
            if _GOVERNED_METHOD.search(message):
                raise WebSocketGatewayCloseError(4400, "zeroth.request_too_large")
            return message
        try:
            payload = json.loads(
                message,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
            )
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError, ValueError):
            return message
        if classify_protocol_command(payload) is not RouteDisposition.GOVERNED:
            return message
        if not isinstance(payload, dict) or not isinstance(payload.get("params"), dict):
            raise WebSocketGatewayCloseError(4400, "zeroth.invalid_request")

        principal = self._principal(websocket)
        params = payload["params"]
        classification: str | None = None
        active_classifier = self._classifier

        class CapturingClassifier:
            async def classify(self, value: object) -> str:
                nonlocal classification
                classification = await active_classifier.classify(value)
                return classification

        request = AdmissionRequest(
            tenant_id=principal.tenant_id,
            principal_id=principal.subject,
            roles=tuple(str(role) for role in principal.roles),
            deployment_ref=self._required_setting("deployment_ref"),
            assistant_id=_optional_identifier(params.get("assistant_id")),
            thread_id=_optional_identifier(websocket.path_params.get("thread_id")),
            operation=str(payload.get("method")),
            input_payload=params.get("input"),
            input_size_bytes=len(raw),
            policy_bindings=self._settings.policy_bindings,
        )
        decision = await admit(
            request,
            policy_guard=self._policy_guard,
            budget_checker=self._budget_checker,
            classifier=CapturingClassifier(),
        )
        if not decision.allowed:
            reason = decision.reason or "zeroth.policy_denied"
            if not reason.startswith("zeroth."):
                reason = "zeroth.policy_denied"
            raise WebSocketGatewayCloseError(4403, reason)
        if self._context_codec is None:
            raise WebSocketGatewayCloseError(4503, "zeroth.context_signing_unavailable")

        issued_at = int(self._clock())
        claims = ReservedContextClaims(
            tenant_id=principal.tenant_id,
            principal_id=principal.subject,
            roles=tuple(str(role) for role in principal.roles),
            deployment_ref=self._required_setting("deployment_ref"),
            audience=self._required_setting("upstream_audience"),
            correlation_id=self._correlation_id(websocket),
            policy_version=decision.policy_version,
            issued_at=issued_at,
            expires_at=issued_at + self._settings.context_ttl_seconds,
            content_classification=classification,
        )
        token = self._context_codec.encode(claims)
        try:
            mutated = inject_reserved_context(
                raw,
                token,
                max_body_bytes=self._settings.max_governed_body_bytes,
                protocol_v2=True,
            )
        except GatewayContextError as exc:
            raise WebSocketGatewayCloseError(_context_close_code(exc), exc.code) from None
        return mutated.decode("utf-8")

    @staticmethod
    def _principal(websocket: WebSocket) -> AuthenticatedPrincipal:
        principal = getattr(websocket.state, "principal", None)
        if not isinstance(principal, AuthenticatedPrincipal):
            raise WebSocketGatewayCloseError(4401, "zeroth.authentication_required")
        return principal

    @staticmethod
    def _correlation_id(websocket: WebSocket) -> str:
        correlation_id = getattr(websocket.state, "correlation_id", None)
        if not isinstance(correlation_id, str) or not correlation_id:
            raise WebSocketGatewayCloseError(4503, "zeroth.gateway_misconfigured")
        return correlation_id

    def _required_setting(self, name: str) -> str:
        value = getattr(self._settings, name)
        if not isinstance(value, str) or not value:
            raise WebSocketGatewayCloseError(4503, "zeroth.gateway_misconfigured")
        return value


class GatewayWebSocketEndpoint:
    """Authenticate a WebSocket scope before accepting or dialing upstream."""

    def __init__(
        self,
        *,
        authenticator: ServiceAuthenticator,
        handler: WebSocketGatewayHandler,
        correlation_factory: Callable[[], str] = lambda: uuid4().hex,
    ) -> None:
        self._authenticator = authenticator
        self._handler = handler
        self._correlation_factory = correlation_factory

    async def __call__(self, websocket: WebSocket) -> None:
        correlation_id = websocket.headers.get(_CORRELATION_HEADER) or self._correlation_factory()
        try:
            principal = self._authenticator.authenticate_headers(websocket.headers)
        except (AuthenticationError, ValidationError):
            await websocket.close(
                code=_AUTHENTICATION_CLOSE_CODE,
                reason="zeroth.authentication_required",
            )
            return
        websocket.state.principal = principal
        websocket.state.correlation_id = correlation_id
        set_correlation_id(correlation_id)
        await self._handler.handle(websocket)


def register_gateway_routes(
    app: FastAPI | APIRouter,
    *,
    proxy: HTTPGatewayProxy,
    websocket_handler: WebSocketGatewayHandler,
    authenticator: ServiceAuthenticator,
    correlation_factory: Callable[[], str] = lambda: uuid4().hex,
) -> None:
    """Register the exact event WebSocket followed by the HTTP catch-all."""
    websocket_endpoint = GatewayWebSocketEndpoint(
        authenticator=authenticator,
        handler=websocket_handler,
        correlation_factory=correlation_factory,
    )

    async def protocol_events(websocket: WebSocket) -> None:
        await websocket_endpoint(websocket)

    async def catch_all(request: Any) -> Any:
        return await proxy.handle_http(request)

    add_api_websocket_route = getattr(app, "add_api_websocket_route", None)
    if callable(add_api_websocket_route):
        add_api_websocket_route(
            "/threads/{thread_id}/stream/events",
            protocol_events,
            name="langgraph-protocol-events",
        )
    else:
        app.add_websocket_route(  # type: ignore[attr-defined]
            "/threads/{thread_id}/stream/events",
            protocol_events,
            name="langgraph-protocol-events",
        )

    methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
    add_api_route = getattr(app, "add_api_route", None)
    if callable(add_api_route):
        add_api_route(
            "/{path:path}",
            catch_all,
            methods=methods,
            include_in_schema=False,
            name="langgraph-gateway",
        )
    else:
        app.add_route(  # type: ignore[attr-defined]
            "/{path:path}",
            catch_all,
            methods=methods,
            name="langgraph-gateway",
        )


def _optional_identifier(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _context_close_code(exc: GatewayContextError) -> int:
    if exc.code == "zeroth.request_too_large":
        return 4400
    if exc.code == "zeroth.context_signing_unavailable":
        return 4503
    return 4400


def _find_gateway_close(
    group: BaseExceptionGroup[BaseException],
) -> WebSocketGatewayCloseError | None:
    for exception in group.exceptions:
        if isinstance(exception, WebSocketGatewayCloseError):
            return exception
        if isinstance(exception, BaseExceptionGroup):
            nested = _find_gateway_close(exception)
            if nested is not None:
                return nested
    return None


def _is_upstream_handshake_failure(exc: Exception) -> bool:
    # websockets intentionally has several handshake exception subclasses;
    # matching their public module keeps this boundary stable across patches.
    return isinstance(exc, (OSError, TimeoutError)) or exc.__class__.__module__.startswith(
        "websockets."
    )


def _websocket_was_accepted(websocket: WebSocket) -> bool:
    accepted = getattr(websocket, "accepted", None)
    if isinstance(accepted, bool):
        return accepted
    application_state = getattr(websocket, "application_state", None)
    return getattr(application_state, "name", None) == "CONNECTED"
