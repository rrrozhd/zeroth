"""Memory connector listing and runtime management REST API.

Surfaces the connector registry populated at bootstrap so operator UIs — the
console's retrieval-node inspector in particular — can offer the actual
resolvable ``connector_ref`` values instead of a blind free-text field.

Also exposes runtime CRUD (POST/PUT/DELETE) so operators can configure
database-backed connectors from the console instead of env files. Runtime
configs are persisted in ``memory_connector_configs`` and re-registered at
bootstrap; secret-bearing params (dsn/url/password) are masked in responses.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import re
import time
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from zeroth.integrations.memory.embedding_calls import EmbeddingReservationMemoryConnector
from zeroth.integrations.memory.factory import build_connector
from zeroth.integrations.memory.governed.models import MemoryScope
from zeroth.platform.primitives.error_vocabulary import safe_error_detail
from zeroth.service.api.authorization import Permission, require_permission

_REF_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
# "scheme://user:pass@host" -> "scheme://***@host": strip the userinfo part
# (credentials) while keeping scheme, host, port, and database visible.
_USERINFO_RE = re.compile(r"://[^@/]+@")

_PROBE_KEY = "zeroth-connection-probe"
_PROBE_TARGET = "__shared__"
_PROBE_TIMEOUT_SECONDS = 5.0


class ConnectorSummaryResponse(BaseModel):
    """One registered memory connector, as shown in the console."""

    model_config = ConfigDict(extra="forbid")

    ref: str
    connector_type: str
    scope: str
    backend: str
    source: str = "env"
    backend_type: str | None = None
    params: dict[str, Any] | None = None


class ConnectorCreateRequest(BaseModel):
    """Payload for creating a runtime-managed connector."""

    model_config = ConfigDict(extra="forbid")

    ref: str
    backend_type: str
    params: dict[str, Any] = Field(default_factory=dict)


class ConnectorUpdateRequest(BaseModel):
    """Payload for reconfiguring an existing runtime-managed connector."""

    model_config = ConfigDict(extra="forbid")

    backend_type: str
    params: dict[str, Any] = Field(default_factory=dict)


class ConnectorTestResponse(BaseModel):
    """Result of a live connectivity probe against a registered connector."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    detail: str | None = None
    latency_ms: float
    campaign_id: str | None = None
    operation_id: str | None = None
    cost_event_id: str | None = None
    audit_event_id: str | None = None
    cost_measurement: str | None = None
    estimated_cost_usd: Decimal | None = None
    provider_request_id: str | None = None
    cleanup_status: str | None = None


class ConnectorTestRequest(BaseModel):
    """Optional cost boundary for a connector test that may call a provider."""

    model_config = ConfigDict(extra="forbid")

    campaign_id: str = Field(min_length=1, max_length=128)
    operation_id: str = Field(min_length=1, max_length=128)
    run_id: str | None = Field(default=None, min_length=1, max_length=128)
    max_cost_usd: Decimal = Field(gt=0)
    run_cap_usd: Decimal | None = Field(default=None, gt=0)


_connector_test_response_parameters = inspect.signature(ConnectorTestResponse).parameters
ConnectorTestResponse.__signature__ = inspect.signature(ConnectorTestResponse).replace(
    parameters=[
        parameter
        for name, parameter in _connector_test_response_parameters.items()
        if name
        not in {
            "campaign_id",
            "operation_id",
            "cost_event_id",
            "audit_event_id",
            "cost_measurement",
            "estimated_cost_usd",
            "provider_request_id",
            "cleanup_status",
        }
    ]
)


def _mask_secret_string(value: str) -> str:
    """Strip credentials userinfo from a connection string, keep host tail."""
    return _USERINFO_RE.sub("://***@", value)


def _mask_params(params: dict[str, Any]) -> dict[str, Any]:
    """Mask secret-bearing values so configs are safe to show in the console."""
    masked: dict[str, Any] = {}
    for key, value in params.items():
        if key == "password":
            masked[key] = "***"
        elif key in ("dsn", "url") and isinstance(value, str):
            masked[key] = _mask_secret_string(value)
        elif key == "hosts" and isinstance(value, list):
            masked[key] = [
                _mask_secret_string(item) if isinstance(item, str) else item for item in value
            ]
        else:
            masked[key] = value
    return masked


def _registry_and_repo(request: Request) -> tuple[Any, Any]:
    bootstrap = request.app.state.bootstrap
    registry = getattr(bootstrap, "memory_registry", None)
    if registry is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="memory connector registry is not available",
        )
    repo = getattr(bootstrap, "memory_connector_config_repository", None)
    return registry, repo


def _require_repo(request: Request) -> tuple[Any, Any]:
    registry, repo = _registry_and_repo(request)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="runtime connector configuration store is not available",
        )
    return registry, repo


def _tenant(request: Request) -> str:
    """Authoritative tenant for this deployment (WS-B).

    A deployment is tenant-pinned, so connector configs are persisted, read,
    and deleted under ``deployment.tenant_id`` — matching what bootstrap loaded
    into this process. Falls back to the reserved sentinel if unset.
    """
    bootstrap = request.app.state.bootstrap
    deployment = getattr(bootstrap, "deployment", None)
    return getattr(deployment, "tenant_id", None) or "default"


def _build_scoped_connector(
    request: Request, backend_type: str, params: dict[str, Any]
) -> tuple[Any, Any]:
    bootstrap = request.app.state.bootstrap
    return build_connector(
        backend_type,
        params,
        secret_provider=getattr(bootstrap, "secret_provider", None),
        tenant_id=_tenant(request),
        allow_env_fallback=getattr(bootstrap, "evaluation_campaign_id", None) is None,
    )


def _validate_ref(ref: str) -> None:
    if not _REF_RE.match(ref):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="ref must match ^[a-z0-9_-]{1,64}$",
        )


def _summary(
    ref: str,
    manifest: Any,
    connector: Any,
    *,
    config: Any | None = None,
) -> ConnectorSummaryResponse:
    return ConnectorSummaryResponse(
        ref=ref,
        connector_type=manifest.connector_type,
        scope=str(getattr(manifest.scope, "value", manifest.scope)),
        backend=type(connector).__name__,
        source="runtime" if config is not None else "env",
        backend_type=config.backend_type if config is not None else None,
        params=_mask_params(config.params) if config is not None else None,
    )


async def _probe_connector(connector: Any) -> None:
    """Cheap real round-trip: write + read a tiny probe entry, then clean up."""
    await connector.write(_PROBE_KEY, "ok", MemoryScope.SHARED, target=_PROBE_TARGET)
    await connector.read(_PROBE_KEY, MemoryScope.SHARED, target=_PROBE_TARGET)
    with contextlib.suppress(Exception):
        await connector.delete(_PROBE_KEY, MemoryScope.SHARED, target=_PROBE_TARGET)


def register_connector_routes(app: FastAPI | APIRouter) -> None:
    """Register memory connector listing and management routes."""

    @app.get("/connectors", response_model=list[ConnectorSummaryResponse])
    async def list_connectors(request: Request) -> list[ConnectorSummaryResponse]:
        """All memory connectors registered for this deployment, by ref.

        These are the values a retrieval node's ``connector_ref`` (and an
        agent's ``memory_refs``) can resolve at run time. Runtime-managed
        connectors include their backend_type and secret-masked params;
        env-sourced connectors return ``params=None``.
        """
        await require_permission(request, Permission.DEPLOYMENT_READ)
        bootstrap = request.app.state.bootstrap
        registry = getattr(bootstrap, "memory_registry", None)
        if registry is None:
            return []
        repo = getattr(bootstrap, "memory_connector_config_repository", None)
        configs = (
            {c.ref: c for c in await repo.list(tenant_id=_tenant(request))}
            if repo is not None
            else {}
        )
        return sorted(
            (
                _summary(ref, manifest, connector, config=configs.get(ref))
                for ref, (manifest, connector) in registry.list().items()
            ),
            key=lambda c: c.ref,
        )

    @app.post(
        "/connectors",
        response_model=ConnectorSummaryResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_connector(
        request: Request, payload: ConnectorCreateRequest
    ) -> ConnectorSummaryResponse:
        """Create a runtime-managed connector: build, persist, then register live.

        Backend construction is cheap and may connect lazily (pgvector opens
        its first connection on first use) -- use POST /connectors/{ref}/test
        for a real connectivity check.

        A02-19: the live registry is an in-process dict and the config store is
        SQL, so no transaction spans them. The ordering is what makes that safe:
        the two steps that can fail come first and leave nothing behind, and the
        one step that cannot fail -- assigning into the registry -- goes last.
        Registering first meant a failed write left a connector serving
        ``connector_ref`` lookups in this process and nowhere else, which
        vanished at the next restart.
        """
        await require_permission(request, Permission.CONNECTOR_ADMIN)
        registry, repo = _require_repo(request)
        tenant = _tenant(request)
        _validate_ref(payload.ref)
        if await repo.get(payload.ref, tenant_id=tenant) is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"runtime connector {payload.ref!r} already exists; use PUT to update",
            )
        if payload.ref in registry.list():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"ref {payload.ref!r} collides with an env-sourced connector",
            )
        try:
            manifest, connector = _build_scoped_connector(
                request, payload.backend_type, payload.params
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
            ) from exc
        config = await repo.upsert(
            payload.ref, payload.backend_type, payload.params, tenant_id=tenant
        )
        registry.register(payload.ref, manifest, connector)
        return _summary(payload.ref, manifest, connector, config=config)

    @app.put("/connectors/{ref}", response_model=ConnectorSummaryResponse)
    async def update_connector(
        request: Request, ref: str, payload: ConnectorUpdateRequest
    ) -> ConnectorSummaryResponse:
        """Reconfigure an existing runtime-managed connector (rebuild + re-register).

        A02-19: same ordering rule as create -- the reconfiguration only goes
        live once it is durable, so a failed write leaves the previous backend
        both live and persisted instead of swapping in one that reverts at the
        next restart.
        """
        await require_permission(request, Permission.CONNECTOR_ADMIN)
        registry, repo = _require_repo(request)
        tenant = _tenant(request)
        if await repo.get(ref, tenant_id=tenant) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"runtime connector {ref!r} not found",
            )
        try:
            manifest, connector = _build_scoped_connector(
                request, payload.backend_type, payload.params
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
            ) from exc
        config = await repo.upsert(ref, payload.backend_type, payload.params, tenant_id=tenant)
        registry.register(ref, manifest, connector)
        return _summary(ref, manifest, connector, config=config)

    @app.delete("/connectors/{ref}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_connector(request: Request, ref: str) -> Response:
        """Delete a runtime-managed connector (env-sourced ones cannot be deleted).

        A02-19: the durable delete goes first for the same reason create's goes
        last -- unregistering first meant a failed row delete left the connector
        unresolvable in this process while its config survived, so the next
        restart resurrected it.
        """
        await require_permission(request, Permission.CONNECTOR_ADMIN)
        registry, repo = _require_repo(request)
        tenant = _tenant(request)
        if await repo.get(ref, tenant_id=tenant) is None:
            if ref in registry.list():
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"connector {ref!r} is env-sourced and cannot be deleted",
                )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"runtime connector {ref!r} not found",
            )
        await repo.delete(ref, tenant_id=tenant)
        registry.unregister(ref)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/connectors/{ref}/test", response_model=ConnectorTestResponse)
    async def test_connector(
        request: Request, ref: str, body: ConnectorTestRequest | None = None
    ) -> ConnectorTestResponse:
        """Probe the LIVE connector with a tiny write+read round-trip.

        Works for both env-sourced and runtime connectors. Connection
        failures never raise 500 -- they come back as ``ok=false`` with the
        error message. The probe is capped at 5 seconds.

        Note: pgvector's write path generates an embedding via litellm, so
        probing a pgvector connector performs one (tiny) embedding API call.
        """
        await require_permission(request, Permission.CONNECTOR_ADMIN)
        strict_campaign_id = getattr(request.app.state.bootstrap, "evaluation_campaign_id", None)
        if strict_campaign_id is not None and (
            body is None or body.campaign_id != strict_campaign_id
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="strict evaluation requires the configured campaign identity",
            )
        registry, _ = _registry_and_repo(request)
        # G9: route the probe through the resolver so it runs behind the
        # TenantScopedMemoryConnector wrapper — two tenants sharing one physical
        # backend must not collide on a single un-namespaced probe cell (the raw
        # connector keyed the probe on the constant SHARED ``__shared__`` target).
        # ``effective_capabilities=None`` keeps the capability gate inactive: this
        # is an operator connectivity check, not an agent memory op, so it must
        # not be fail-closed. Falls back to the raw connector only when no
        # resolver is wired (a bootstrap without the memory plane).
        resolver = getattr(request.app.state.bootstrap, "memory_resolver", None)
        try:
            if resolver is not None:
                bindings = await resolver.resolve(
                    [ref],
                    runtime_context={"tenant_id": _tenant(request)},
                    effective_capabilities=None,
                )
                connector = bindings[0].connector
            else:
                _, connector = registry.resolve(ref)
        except KeyError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"connector {ref!r} not found",
            ) from None
        instrumentation = None
        embedding_model = None
        embedding_hooks = None
        if body is not None:
            instrumentation = getattr(request.app.state.bootstrap, "probe_instrumentation", None)
            if instrumentation is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="cost reservation control plane unavailable; failing closed",
                )
            underlying = connector
            seen: set[int] = set()
            while id(underlying) not in seen:
                seen.add(id(underlying))
                embedding_model = getattr(underlying, "_embedding_model", None)
                if embedding_model is not None:
                    break
                nested = getattr(underlying, "_inner", None) or getattr(
                    underlying, "_connector", None
                )
                if nested is None:
                    break
                underlying = nested
            server_max_cost = Decimal("0")
            if embedding_model is not None:
                estimator = getattr(request.app.state.bootstrap, "cost_estimator", None)
                if estimator is None:
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="connector probe maximum cannot be priced; failing closed",
                    )
                server_max_cost = estimator.estimate(
                    embedding_model, input_tokens=256, output_tokens=0
                )
                if server_max_cost is None or server_max_cost <= 0:
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="connector probe maximum is unknown; failing closed",
                    )
            if server_max_cost > body.max_cost_usd:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="server-calculated connector maximum exceeds acknowledged ceiling",
                )
            server_run_cap = getattr(
                getattr(request.app.state.bootstrap, "orchestrator", None),
                "per_run_cap_usd",
                None,
            )
            if server_run_cap is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="server-owned per-run ceiling is not configured; failing closed",
                )
            if body.run_cap_usd is not None and body.run_cap_usd != server_run_cap:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="requested run ceiling does not match the server-owned ceiling",
                )
            try:
                await instrumentation.reserve_probe(
                    tenant_id=_tenant(request),
                    campaign_id=body.campaign_id,
                    operation_id=body.operation_id,
                    run_id=body.run_id,
                    max_cost_usd=str(server_max_cost),
                    run_cap_usd=str(server_run_cap),
                    capability_id="connector.probe",
                    # The execution event is emitted by the embedding hook
                    # under the model identity. Register that same identity at
                    # admission so confirmed Regulus delivery cannot violate
                    # the implementation foreign key after the provider call.
                    implementation_id=embedding_model or ref,
                )
            except Exception as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="cost reservation refused or unavailable; failing closed",
                ) from exc
            if embedding_model is not None:
                from zeroth.service.probe_instrumentation import (
                    ReservedProbeEmbeddingInstrumentation,
                )

                embedding_hooks = ReservedProbeEmbeddingInstrumentation(
                    instrumentation=instrumentation,
                    cost_estimator=estimator,
                    tenant_id=_tenant(request),
                    campaign_id=body.campaign_id,
                    operation_id=body.operation_id,
                    run_id=body.run_id,
                    capability_id="connector.probe",
                    implementation_id=embedding_model,
                )
                connector = EmbeddingReservationMemoryConnector(
                    connector,
                    hooks=embedding_hooks,
                    tenant_id=_tenant(request),
                    run_id=body.run_id or f"probe:{body.operation_id}",
                    node_id="connector-probe",
                    campaign_id=body.campaign_id,
                    strict=True,
                )
        start = time.perf_counter()
        ok = True
        detail: str | None = None
        try:
            await asyncio.wait_for(_probe_connector(connector), timeout=_PROBE_TIMEOUT_SECONDS)
        except TimeoutError:
            ok = False
            detail = f"probe timed out after {_PROBE_TIMEOUT_SECONDS:g}s"
        except Exception as exc:  # noqa: BLE001 - surface as ok=false, never 500
            ok = False
            # A02-8: a memory-backend driver's message names the host, port, and
            # often the DSN it was constructed from. The operator needs to know
            # WHY the probe failed, not the connection string it failed against.
            detail = safe_error_detail(exc, context="connector probe")
        latency_ms = (time.perf_counter() - start) * 1000.0
        evidence = None
        if body is not None and instrumentation is not None:
            try:
                if embedding_model is None:
                    evidence = await instrumentation.release_probe(
                        tenant_id=_tenant(request),
                        operation_id=body.operation_id,
                        cleanup_status="complete",
                    )
                elif embedding_hooks is not None and embedding_hooks.evidence is not None:
                    evidence = embedding_hooks.evidence
                else:
                    # The connector failed before reaching its provider boundary.
                    # No provider outcome exists, so the full reservation can be
                    # released instead of being mislabeled ambiguous.
                    evidence = await instrumentation.release_probe(
                        tenant_id=_tenant(request),
                        operation_id=body.operation_id,
                        cleanup_status="provider_not_called",
                    )
            except Exception as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="probe reconciliation persistence failed; reservation retained",
                ) from exc
        return ConnectorTestResponse(
            ok=ok,
            detail=detail,
            latency_ms=round(latency_ms, 2),
            campaign_id=body.campaign_id if body is not None else None,
            operation_id=body.operation_id if body is not None else None,
            cost_event_id=getattr(evidence, "cost_event_id", None),
            audit_event_id=(f"audit_{evidence.cost_event_id}" if evidence is not None else None),
            cost_measurement=getattr(evidence, "cost_measurement", None),
            estimated_cost_usd=(
                embedding_hooks.estimated_cost_usd
                if embedding_hooks is not None
                and getattr(evidence, "cost_measurement", None) == "estimated"
                else None
            ),
            provider_request_id=getattr(evidence, "provider_request_id", None),
            cleanup_status=getattr(evidence, "cleanup_status", None),
        )
