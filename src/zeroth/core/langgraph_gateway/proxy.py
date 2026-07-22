"""Governed Agent Server HTTP proxy pipeline and stable gateway errors."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

import httpx
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from zeroth.core.config.settings import LangGraphGatewaySettings
from zeroth.core.identity import AuthenticatedPrincipal
from zeroth.core.langgraph_gateway.admission import (
    BudgetChecker,
    InputClassifier,
    PolicyAdmissionChecker,
    UnclassifiedInputClassifier,
    admit,
)
from zeroth.core.langgraph_gateway.compatibility import CompatibilityResult
from zeroth.core.langgraph_gateway.context import (
    GatewayContextError,
    ReservedContextClaims,
    ReservedContextCodec,
    inject_reserved_context,
)
from zeroth.core.langgraph_gateway.events import TeeObserver
from zeroth.core.langgraph_gateway.headers import UpstreamCredentialUnavailableError
from zeroth.core.langgraph_gateway.inventory import (
    EndpointRule,
    classify_endpoint,
    classify_protocol_command,
)
from zeroth.core.langgraph_gateway.models import (
    AdmissionDecision,
    AdmissionRequest,
    CompatibilityStatus,
    EndpointKind,
    GatewayCorrelation,
    GatewayError,
    GatewayEvent,
    GatewayEventStatus,
    GovernanceLevel,
    RouteDisposition,
)
from zeroth.core.langgraph_gateway.transport import HTTPGatewayTransport
from zeroth.core.service.auth import current_principal

logger = logging.getLogger(__name__)

_CORRELATION_HEADER = "X-Correlation-ID"
_GOVERNANCE_HEADER = "X-Zeroth-Governance-Level"
_GOVERNANCE_MODE_HEADER = "X-Zeroth-Governance"


class GatewayEventSink(Protocol):
    async def emit(self, event: GatewayEvent) -> None: ...


class RouteClassifier(Protocol):
    def __call__(self, method: str, path: str) -> EndpointRule | None: ...


class PrincipalResolver(Protocol):
    def __call__(self, request: Request) -> AuthenticatedPrincipal: ...


class GatewayProxy:
    """Apply exact admission and context mutation before streaming upstream."""

    def __init__(
        self,
        *,
        settings: LangGraphGatewaySettings,
        transport: HTTPGatewayTransport,
        context_codec: ReservedContextCodec | None,
        policy_guard: PolicyAdmissionChecker,
        budget_checker: BudgetChecker,
        compatibility: CompatibilityResult,
        classifier: InputClassifier | None = None,
        event_sink: GatewayEventSink | None = None,
        principal_resolver: PrincipalResolver = current_principal,
        route_classifier: RouteClassifier = classify_endpoint,
        clock: Callable[[], int | float] = time.time,
        event_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        correlation_factory: Callable[[], str] = lambda: uuid4().hex,
        max_observation_bytes: int = 65_536,
        event_sink_timeout_seconds: int | float = 0.1,
    ) -> None:
        if (
            isinstance(event_sink_timeout_seconds, bool)
            or not isinstance(event_sink_timeout_seconds, (int, float))
            or event_sink_timeout_seconds <= 0
        ):
            raise ValueError("event_sink_timeout_seconds must be positive")
        self._settings = settings
        self._transport = transport
        self._context_codec = context_codec
        self._policy_guard = policy_guard
        self._budget_checker = budget_checker
        self._classifier = classifier or UnclassifiedInputClassifier()
        self._compatibility = compatibility
        self._event_sink = event_sink
        self._principal_resolver = principal_resolver
        self._route_classifier = route_classifier
        self._clock = clock
        self._event_clock = event_clock
        self._correlation_factory = correlation_factory
        self._max_observation_bytes = max_observation_bytes
        self._event_sink_timeout_seconds = float(event_sink_timeout_seconds)
        self.sink_failure_count = 0

    async def handle_http(self, request: Request) -> Response:
        """Return an upstream response or a stable, gateway-owned error."""
        correlation_id = self._correlation_id(request)
        started_at = self._event_clock()
        rule = self._route_classifier(request.method, request.url.path)
        principal: AuthenticatedPrincipal | None = None
        disposition = rule.disposition if rule is not None else RouteDisposition.UNSUPPORTED
        operation = rule.operation if rule is not None else "unsupported.endpoint"
        decision: AdmissionDecision | None = None
        input_sha256: str | None = None
        input_size_bytes: int | None = None
        request_identifiers = _path_identifiers(rule, request.url.path)

        try:
            principal = self._principal_resolver(request)
            if rule is None:
                if self._settings.unknown_endpoint_mode == "deny":
                    return await self._denial_response(
                        code="zeroth.unsupported_endpoint",
                        status_code=501,
                        reason="the endpoint is outside the supported gateway inventory",
                        correlation_id=correlation_id,
                        principal=principal,
                        operation=operation,
                        disposition=RouteDisposition.UNSUPPORTED,
                        started_at=started_at,
                        identifiers=request_identifiers,
                    )
                compatibility_error = self._compatibility_error(correlation_id)
                if compatibility_error is not None:
                    status_code, error = compatibility_error
                    await self._emit_terminal(
                        principal=principal,
                        correlation_id=correlation_id,
                        operation=operation,
                        disposition=RouteDisposition.UNSUPPORTED,
                        status=GatewayEventStatus.GATEWAY_DENIAL,
                        started_at=started_at,
                        identifiers=request_identifiers,
                    )
                    return self._error_response(error, status_code)
                logger.warning(
                    "forwarding ungoverned LangGraph endpoint",
                    extra={"correlation_id": correlation_id, "operation": operation},
                )
                return await self._forward(
                    request,
                    principal=principal,
                    correlation_id=correlation_id,
                    operation=operation,
                    disposition=RouteDisposition.UNSUPPORTED,
                    started_at=started_at,
                    request_identifiers=request_identifiers,
                    governance_header=(_GOVERNANCE_MODE_HEADER, "ungoverned"),
                )

            compatibility_error = self._compatibility_error(correlation_id)
            if compatibility_error is not None:
                status_code, error = compatibility_error
                await self._emit_terminal(
                    principal=principal,
                    correlation_id=correlation_id,
                    operation=operation,
                    disposition=disposition or RouteDisposition.UNSUPPORTED,
                    status=GatewayEventStatus.GATEWAY_DENIAL,
                    started_at=started_at,
                    identifiers=request_identifiers,
                )
                return self._error_response(error, status_code)

            forwarded_request = request
            if (
                rule.kind == EndpointKind.PROTOCOL_COMMAND
                or disposition == RouteDisposition.GOVERNED
            ):
                raw_body, payload = await self._read_governed_json(request)
                input_sha256 = hashlib.sha256(raw_body).hexdigest()
                input_size_bytes = len(raw_body)
                if rule.kind == EndpointKind.PROTOCOL_COMMAND:
                    # Body-dependent protocol classification necessarily consumes
                    # the client stream; rebuild it even when the command is read-only.
                    forwarded_request = _request_with_body(request, raw_body)
                    disposition = classify_protocol_command(payload)
                    if disposition == RouteDisposition.UNSUPPORTED:
                        return await self._denial_response(
                            code="zeroth.unsupported_endpoint",
                            status_code=501,
                            reason=(
                                "the protocol command is outside the supported gateway inventory"
                            ),
                            correlation_id=correlation_id,
                            principal=principal,
                            operation=operation,
                            disposition=disposition,
                            started_at=started_at,
                            identifiers=request_identifiers,
                            input_sha256=input_sha256,
                            input_size_bytes=input_size_bytes,
                        )
                if disposition == RouteDisposition.GOVERNED:
                    target = (
                        payload.get("params")
                        if rule.kind == EndpointKind.PROTOCOL_COMMAND
                        else payload
                    )
                    if not isinstance(target, dict):
                        raise GatewayContextError(
                            "zeroth.invalid_request", "governed request is invalid"
                        )
                    classification: str | None = None
                    active_classifier = self._classifier

                    class CapturingClassifier:
                        async def classify(self, value: object) -> str:
                            nonlocal classification
                            classification = await active_classifier.classify(value)
                            return classification

                    admission_request = AdmissionRequest(
                        tenant_id=principal.tenant_id,
                        principal_id=principal.subject,
                        roles=tuple(str(role) for role in principal.roles),
                        deployment_ref=self._required_setting("deployment_ref"),
                        assistant_id=_optional_identifier(target.get("assistant_id")),
                        thread_id=request_identifiers.get("thread_id"),
                        operation=operation,
                        input_payload=target.get("input"),
                        input_size_bytes=len(raw_body),
                        policy_bindings=self._settings.policy_bindings,
                    )
                    request_identifiers.update(
                        {
                            key: value
                            for key, value in {
                                "assistant_id": admission_request.assistant_id,
                                "run_id": _optional_identifier(target.get("run_id")),
                            }.items()
                            if value is not None
                        }
                    )
                    decision = await admit(
                        admission_request,
                        policy_guard=self._policy_guard,
                        budget_checker=self._budget_checker,
                        classifier=CapturingClassifier(),
                    )
                    if not decision.allowed:
                        code = decision.reason or "zeroth.policy_denied"
                        return await self._denial_response(
                            code=code if code.startswith("zeroth.") else "zeroth.policy_denied",
                            status_code=403,
                            reason=_safe_admission_reason(code),
                            correlation_id=correlation_id,
                            principal=principal,
                            operation=operation,
                            disposition=disposition,
                            started_at=started_at,
                            decision=decision,
                            identifiers=request_identifiers,
                            input_sha256=input_sha256,
                            input_size_bytes=input_size_bytes,
                        )
                    if self._context_codec is None:
                        raise GatewayContextError(
                            "zeroth.context_signing_unavailable",
                            "reserved context signing is unavailable",
                        )
                    issued_at = int(self._clock())
                    claims = ReservedContextClaims(
                        tenant_id=principal.tenant_id,
                        principal_id=principal.subject,
                        roles=tuple(str(role) for role in principal.roles),
                        deployment_ref=self._required_setting("deployment_ref"),
                        audience=self._required_setting("upstream_audience"),
                        correlation_id=correlation_id,
                        policy_version=decision.policy_version,
                        issued_at=issued_at,
                        expires_at=issued_at + self._settings.context_ttl_seconds,
                        content_classification=classification,
                    )
                    token = self._context_codec.encode(claims)
                    mutated = inject_reserved_context(
                        raw_body,
                        token,
                        max_body_bytes=self._settings.max_governed_body_bytes,
                        protocol_v2=rule.kind == EndpointKind.PROTOCOL_COMMAND,
                    )
                    forwarded_request = _request_with_body(request, mutated)

            governance_header = (
                (_GOVERNANCE_HEADER, GovernanceLevel.ADMISSION.value)
                if disposition == RouteDisposition.GOVERNED
                else None
            )
            return await self._forward(
                forwarded_request,
                principal=principal,
                correlation_id=correlation_id,
                operation=operation,
                disposition=disposition or RouteDisposition.TRANSPARENT,
                started_at=started_at,
                decision=decision,
                input_sha256=input_sha256,
                input_size_bytes=input_size_bytes,
                request_identifiers=request_identifiers,
                governance_header=governance_header,
            )
        except asyncio.CancelledError:
            if principal is not None:
                await self._emit_terminal(
                    principal=principal,
                    correlation_id=correlation_id,
                    operation=operation,
                    disposition=disposition or RouteDisposition.UNSUPPORTED,
                    status=GatewayEventStatus.CANCELLATION,
                    started_at=started_at,
                    decision=decision,
                    identifiers=request_identifiers,
                    input_sha256=input_sha256,
                    input_size_bytes=input_size_bytes,
                )
            raise
        except GatewayContextError as exc:
            status_code = (
                413
                if exc.code == "zeroth.request_too_large"
                else 503
                if exc.code == "zeroth.context_signing_unavailable"
                else 400
            )
            return await self._caught_error(
                exc.code,
                status_code,
                exc.reason,
                retryable=status_code == 503,
                correlation_id=correlation_id,
                principal=principal,
                operation=operation,
                disposition=disposition or RouteDisposition.UNSUPPORTED,
                started_at=started_at,
                decision=decision,
                identifiers=request_identifiers,
                input_sha256=input_sha256,
                input_size_bytes=input_size_bytes,
            )
        except UpstreamCredentialUnavailableError:
            return await self._caught_error(
                "zeroth.upstream_credential_unavailable",
                503,
                "the configured upstream credential is unavailable",
                retryable=True,
                correlation_id=correlation_id,
                principal=principal,
                operation=operation,
                disposition=disposition or RouteDisposition.UNSUPPORTED,
                started_at=started_at,
                decision=decision,
                identifiers=request_identifiers,
                input_sha256=input_sha256,
                input_size_bytes=input_size_bytes,
            )
        except (httpx.TimeoutException, TimeoutError):
            return await self._caught_error(
                "zeroth.upstream_timeout",
                504,
                "the upstream Agent Server timed out",
                retryable=True,
                correlation_id=correlation_id,
                principal=principal,
                operation=operation,
                disposition=disposition or RouteDisposition.UNSUPPORTED,
                started_at=started_at,
                decision=decision,
                identifiers=request_identifiers,
                input_sha256=input_sha256,
                input_size_bytes=input_size_bytes,
            )
        except httpx.HTTPError:
            return await self._caught_error(
                "zeroth.upstream_unavailable",
                502,
                "the upstream Agent Server is unavailable",
                retryable=True,
                correlation_id=correlation_id,
                principal=principal,
                operation=operation,
                disposition=disposition or RouteDisposition.UNSUPPORTED,
                started_at=started_at,
                decision=decision,
                identifiers=request_identifiers,
                input_sha256=input_sha256,
                input_size_bytes=input_size_bytes,
            )
        except RuntimeError:
            return await self._caught_error(
                "zeroth.gateway_misconfigured",
                503,
                "the gateway is unavailable",
                retryable=True,
                correlation_id=correlation_id,
                principal=principal,
                operation=operation,
                disposition=disposition or RouteDisposition.UNSUPPORTED,
                started_at=started_at,
                decision=decision,
                identifiers=request_identifiers,
                input_sha256=input_sha256,
                input_size_bytes=input_size_bytes,
            )

    async def _read_governed_json(self, request: Request) -> tuple[bytes, dict[str, Any]]:
        limit = self._settings.max_governed_body_bytes
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > limit:
                    raise GatewayContextError(
                        "zeroth.request_too_large", "governed request body is too large"
                    )
            except ValueError:
                raise GatewayContextError(
                    "zeroth.invalid_request", "governed request is invalid"
                ) from None
        body = bytearray()
        try:
            async for chunk in request.stream():
                if len(body) + len(chunk) > limit:
                    raise GatewayContextError(
                        "zeroth.request_too_large", "governed request body is too large"
                    )
                body.extend(chunk)
            payload = json.loads(
                body,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
            )
        except GatewayContextError:
            raise
        except (
            ClientDisconnect,
            UnicodeDecodeError,
            json.JSONDecodeError,
            RecursionError,
            ValueError,
        ):
            raise GatewayContextError(
                "zeroth.invalid_request", "governed request is invalid"
            ) from None
        if not isinstance(payload, dict):
            raise GatewayContextError("zeroth.invalid_request", "governed request is invalid")
        return bytes(body), payload

    async def _forward(
        self,
        request: Request,
        *,
        principal: AuthenticatedPrincipal,
        correlation_id: str,
        operation: str,
        disposition: RouteDisposition,
        started_at: datetime,
        decision: AdmissionDecision | None = None,
        input_sha256: str | None = None,
        input_size_bytes: int | None = None,
        request_identifiers: dict[str, str] | None = None,
        governance_header: tuple[str, str] | None = None,
    ) -> StreamingResponse:
        response = await self._transport.forward(request, tenant_id=principal.tenant_id)
        response.headers[_CORRELATION_HEADER] = correlation_id
        if governance_header is not None:
            response.headers[governance_header[0]] = governance_header[1]
        observer = TeeObserver(
            response.headers.get("content-type"),
            max_observation_bytes=self._max_observation_bytes,
        )
        original_iterator = response.body_iterator
        response.body_iterator = self._observe_body(
            original_iterator,
            observer=observer,
            principal=principal,
            correlation_id=correlation_id,
            operation=operation,
            disposition=disposition,
            started_at=started_at,
            decision=decision,
            input_sha256=input_sha256,
            input_size_bytes=input_size_bytes,
            request_identifiers=request_identifiers,
            upstream_status_code=response.status_code,
        )
        return response

    async def _observe_body(
        self,
        body: AsyncIterator[bytes],
        *,
        observer: TeeObserver,
        principal: AuthenticatedPrincipal,
        correlation_id: str,
        operation: str,
        disposition: RouteDisposition,
        started_at: datetime,
        decision: AdmissionDecision | None,
        input_sha256: str | None,
        input_size_bytes: int | None,
        request_identifiers: dict[str, str] | None,
        upstream_status_code: int,
    ) -> AsyncIterator[bytes]:
        status: GatewayEventStatus | None = None
        try:
            async for chunk in body:
                yield observer.observe(chunk)
            status = (
                GatewayEventStatus.SUCCESS
                if upstream_status_code < 400
                else GatewayEventStatus.UPSTREAM_ERROR
            )
        except asyncio.CancelledError:
            status = GatewayEventStatus.CANCELLATION
            raise
        except GeneratorExit:
            status = GatewayEventStatus.CLIENT_DISCONNECT
            raise
        except BaseException:
            status = GatewayEventStatus.UPSTREAM_ERROR
            raise
        finally:
            observer.finish()
            identifiers = dict(request_identifiers or {})
            identifiers.update(observer.identifiers)
            await self._emit_terminal(
                principal=principal,
                correlation_id=correlation_id,
                operation=operation,
                disposition=disposition,
                status=status or GatewayEventStatus.CLIENT_DISCONNECT,
                started_at=started_at,
                decision=decision,
                identifiers=identifiers,
                input_sha256=input_sha256,
                input_size_bytes=input_size_bytes,
                output_sha256=observer.output_sha256,
                output_size_bytes=observer.output_size_bytes,
                upstream_status_code=upstream_status_code,
            )

    async def _denial_response(
        self, *, code: str, status_code: int, reason: str, **event: Any
    ) -> JSONResponse:
        return await self._caught_error(code, status_code, reason, retryable=False, **event)

    async def _caught_error(
        self,
        code: str,
        status_code: int,
        reason: str,
        *,
        retryable: bool,
        correlation_id: str,
        principal: AuthenticatedPrincipal | None,
        operation: str,
        disposition: RouteDisposition,
        started_at: datetime,
        decision: AdmissionDecision | None = None,
        identifiers: dict[str, str] | None = None,
        input_sha256: str | None = None,
        input_size_bytes: int | None = None,
    ) -> JSONResponse:
        if principal is not None:
            await self._emit_terminal(
                principal=principal,
                correlation_id=correlation_id,
                operation=operation,
                disposition=disposition,
                status=GatewayEventStatus.GATEWAY_DENIAL,
                started_at=started_at,
                decision=decision,
                identifiers=identifiers,
                input_sha256=input_sha256,
                input_size_bytes=input_size_bytes,
            )
        return self._error_response(
            GatewayError(
                code=code,
                correlation_id=correlation_id,
                retryable=retryable,
                reason=reason,
            ),
            status_code,
        )

    async def _emit_terminal(
        self,
        *,
        principal: AuthenticatedPrincipal,
        correlation_id: str,
        operation: str,
        disposition: RouteDisposition,
        status: GatewayEventStatus,
        started_at: datetime,
        decision: AdmissionDecision | None = None,
        identifiers: dict[str, str] | None = None,
        input_sha256: str | None = None,
        input_size_bytes: int | None = None,
        output_sha256: str | None = None,
        output_size_bytes: int | None = None,
        upstream_status_code: int | None = None,
    ) -> None:
        if self._event_sink is None:
            return
        identifiers = identifiers or {}
        event = GatewayEvent(
            correlation=GatewayCorrelation(
                correlation_id=correlation_id,
                deployment_ref=self._settings.deployment_ref or "unconfigured",
                tenant_id=principal.tenant_id,
                principal_id=principal.subject,
                assistant_id=identifiers.get("assistant_id"),
                thread_id=identifiers.get("thread_id"),
                run_id=identifiers.get("run_id"),
            ),
            operation=operation,
            disposition=disposition,
            governance_level=GovernanceLevel.ADMISSION,
            status=status,
            started_at=started_at,
            completed_at=self._event_clock(),
            policy_version=decision.policy_version if decision else None,
            budget_spend_usd=decision.budget_spend_usd if decision else None,
            budget_cap_usd=decision.budget_cap_usd if decision else None,
            budget_check_degraded=decision.budget_check_degraded if decision else False,
            compatibility_fingerprint=self._compatibility.openapi_fingerprint,
            input_sha256=input_sha256,
            input_size_bytes=input_size_bytes,
            output_sha256=output_sha256,
            output_size_bytes=output_size_bytes,
            upstream_status_code=upstream_status_code,
        )
        try:
            async with asyncio.timeout(self._event_sink_timeout_seconds):
                await self._event_sink.emit(event)
        except TimeoutError:
            self.sink_failure_count += 1
            logger.warning(
                "LangGraph gateway event sink timed out",
                extra={"correlation_id": correlation_id},
            )
        except Exception:  # noqa: BLE001 - audit is deliberately best effort.
            self.sink_failure_count += 1
            logger.exception(
                "LangGraph gateway event sink failed", extra={"correlation_id": correlation_id}
            )

    def _compatibility_error(self, correlation_id: str) -> tuple[int, GatewayError] | None:
        if self._compatibility.status == CompatibilityStatus.SUPPORTED:
            return None
        unavailable = self._compatibility.status == CompatibilityStatus.UNAVAILABLE
        return (
            503 if unavailable else 501,
            GatewayError(
                code="zeroth.upstream_unavailable"
                if unavailable
                else "zeroth.unsupported_upstream",
                correlation_id=correlation_id,
                retryable=unavailable,
                reason=(
                    "the upstream Agent Server is unavailable"
                    if unavailable
                    else "the upstream Agent Server version is unsupported"
                ),
            ),
        )

    @staticmethod
    def _error_response(error: GatewayError, status_code: int) -> JSONResponse:
        response = JSONResponse(error.model_dump(mode="json"), status_code=status_code)
        response.headers[_CORRELATION_HEADER] = error.correlation_id
        return response

    def _correlation_id(self, request: Request) -> str:
        existing = request.headers.get(_CORRELATION_HEADER)
        return existing if existing else self._correlation_factory()

    def _required_setting(self, name: str) -> str:
        value = getattr(self._settings, name)
        if not isinstance(value, str) or not value:
            raise RuntimeError("gateway identity is not configured")
        return value


def _request_with_body(request: Request, body: bytes) -> Request:
    sent = False

    async def receive() -> dict[str, object]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    scope = dict(request.scope)
    headers = [
        (name, value) for name, value in request.headers.raw if name.lower() != b"content-length"
    ]
    headers.append((b"content-length", str(len(body)).encode("ascii")))
    scope["headers"] = headers
    return Request(scope, receive)


def _optional_identifier(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _path_identifiers(rule: EndpointRule | None, path: str) -> dict[str, str]:
    """Extract identifiers only from declared placeholders in a matched rule."""
    if rule is None:
        return {}
    template_parts = rule.path_template.strip("/").split("/")
    path_parts = path.strip("/").split("/")
    if len(template_parts) != len(path_parts):
        return {}
    identifiers: dict[str, str] = {}
    identifier_placeholders = {
        "{thread_id}": "thread_id",
        "{assistant_id}": "assistant_id",
        "{run_id}": "run_id",
    }
    for template_part, path_part in zip(template_parts, path_parts, strict=True):
        key = identifier_placeholders.get(template_part)
        if key is not None and path_part:
            identifiers[key] = path_part
    return identifiers


def _safe_admission_reason(code: str) -> str:
    if code == "zeroth.budget_denied":
        return "the configured budget denied this run"
    if code == "zeroth.budget_unavailable":
        return "budget admission is unavailable"
    if code == "zeroth.policy_unavailable":
        return "policy admission is unavailable"
    if code == "zeroth.classifier_unavailable":
        return "input classification is unavailable"
    return "policy denied this run"


__all__ = ["GatewayEventSink", "GatewayProxy"]
