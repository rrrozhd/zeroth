"""Health probe endpoints for container orchestration.

Provides readiness and liveness probes with per-dependency status
checking for database, Redis, and Regulus.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from typing import TYPE_CHECKING

import httpx
from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from redis.asyncio import from_url as redis_from_url

from zeroth.contracts.langgraph_gateway.models import CompatibilityResult, GovernanceLevel
from zeroth.governance.audit.delivery import AuditDeliveryQueue
from zeroth.platform.primitives.error_vocabulary import safe_error_detail
from zeroth.platform.storage.schema_revision import (
    SchemaRevision,
    read_async_schema_revision,
    unknown_schema_revision,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

    from zeroth.platform.storage.database import AsyncDatabase

SERVICE_MIGRATIONS_PACKAGE = "zeroth.service._migrations"
SCHEMA_REVISION_TIMEOUT_SECONDS = 2.0


class DependencyStatus(BaseModel):
    """Status of a single dependency check."""

    model_config = ConfigDict(extra="forbid")

    status: str  # "ok" | "unavailable" | "error"
    latency_ms: float | None = None
    detail: str | None = None


class ReadinessResponse(BaseModel):
    """Response payload for the readiness probe."""

    model_config = ConfigDict(extra="forbid")

    status: str  # "ok" | "degraded" | "unhealthy"
    checks: dict[str, DependencyStatus] = Field(default_factory=dict)
    schema_revision: SchemaRevision

    @model_validator(mode="after")
    def _current_schema_when_ready(self) -> ReadinessResponse:
        if self.status == "ok" and self.schema_revision.state != "current":
            raise ValueError("ok readiness requires current schema revision evidence")
        return self


class LivenessResponse(BaseModel):
    """Response payload for the liveness probe."""

    model_config = ConfigDict(extra="forbid")

    status: str = "ok"


def determine_readiness_status(
    checks: dict[str, DependencyStatus],
    schema_revision: SchemaRevision | None = None,
) -> str:
    """Determine overall readiness status from per-dependency checks.

    Rules:
    - If the database is not "ok", or a *configured* redis errors -> "unhealthy"
      (redis "unavailable" means not configured/disabled — informational only)
    - If only regulus has status != "ok" -> "degraded"
    - If audit delivery is losing events -> "degraded" (never "ok")
    - Otherwise -> "ok"
    """
    db_status = checks.get("database")
    redis_status = checks.get("redis")
    regulus_status = checks.get("regulus")
    agent_server_status = checks.get("agent_server")
    audit_delivery_status = checks.get("audit_delivery")

    if (db_status and db_status.status != "ok") or (
        redis_status and redis_status.status not in {"ok", "unavailable"}
    ):
        return "unhealthy"

    if schema_revision is not None and schema_revision.state != "current":
        return "degraded"

    if regulus_status and regulus_status.status != "ok":
        return "degraded"

    if agent_server_status and agent_server_status.status != "supported":
        return "degraded"

    # Losing audit evidence does not stop the service from answering requests,
    # so it degrades rather than fails the probe -- but it is never "ok".
    if audit_delivery_status and audit_delivery_status.status != "ok":
        return "degraded"

    return "ok"


async def check_database(db: AsyncDatabase) -> DependencyStatus:
    """Check database connectivity by executing SELECT 1.

    A02-4: the failure is reported as a category, never as the driver's own text.
    ``/health/ready`` answers before authentication AND returns 200 even when a
    dependency is down, so a driver message here -- which carries the DSN, host,
    and port it was constructed from -- reaches an unauthenticated caller inside a
    success response.
    """
    start = time.monotonic()
    try:
        async with db.transaction() as conn:
            await conn.fetch_one("SELECT 1")
        elapsed_ms = (time.monotonic() - start) * 1000
        return DependencyStatus(status="ok", latency_ms=elapsed_ms)
    except Exception as exc:
        elapsed_ms = (time.monotonic() - start) * 1000
        return DependencyStatus(
            status="error",
            latency_ms=elapsed_ms,
            detail=safe_error_detail(exc, context="database"),
        )


async def check_schema_revision(
    db: AsyncDatabase,
    *,
    timeout_seconds: float = SCHEMA_REVISION_TIMEOUT_SECONDS,
) -> SchemaRevision:
    """Read service migration evidence within a hard request-time bound."""
    try:
        async with asyncio.timeout(timeout_seconds):
            return await read_async_schema_revision(db, SERVICE_MIGRATIONS_PACKAGE)
    except TimeoutError:
        return unknown_schema_revision(SERVICE_MIGRATIONS_PACKAGE)


async def check_redis(redis_url: str | None) -> DependencyStatus:
    """Check Redis connectivity by issuing a PING command."""
    if redis_url is None:
        return DependencyStatus(status="unavailable")

    start = time.monotonic()
    try:
        client = redis_from_url(redis_url)
        try:
            await client.ping()
            elapsed_ms = (time.monotonic() - start) * 1000
            return DependencyStatus(status="ok", latency_ms=elapsed_ms)
        finally:
            await client.aclose()
    except Exception as exc:
        elapsed_ms = (time.monotonic() - start) * 1000
        return DependencyStatus(
            status="error",
            latency_ms=elapsed_ms,
            detail=safe_error_detail(exc, context="redis"),
        )


async def check_regulus(base_url: str | None, timeout: float = 5.0) -> DependencyStatus:
    """Check Regulus service availability via its health endpoint."""
    if base_url is None:
        return DependencyStatus(status="unavailable")

    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            await client.get(f"{base_url}/health")
        elapsed_ms = (time.monotonic() - start) * 1000
        return DependencyStatus(status="ok", latency_ms=elapsed_ms)
    except Exception:
        elapsed_ms = (time.monotonic() - start) * 1000
        return DependencyStatus(status="unavailable", latency_ms=elapsed_ms)


def register_health_routes(app: FastAPI) -> None:
    """Register /health/ready and /health/live endpoints on the app."""

    @app.get("/health/ready", response_model=ReadinessResponse)
    async def health_ready(request: Request) -> ReadinessResponse:
        bootstrap = request.app.state.bootstrap

        # Gather database reference from bootstrap.
        database = getattr(bootstrap, "database", None)

        # Build Redis URL from settings if available.
        redis_url: str | None = None
        try:
            from zeroth.platform.config.settings import get_settings

            settings = get_settings()
            rs = settings.redis
            if rs.mode != "disabled":
                scheme = "rediss" if rs.tls else "redis"
                auth = f":{rs.password.get_secret_value()}@" if rs.password else ""
                redis_url = f"{scheme}://{auth}{rs.host}:{rs.port}/{rs.db}"
        except Exception:
            pass

        # Get Regulus base URL.
        regulus_base_url: str | None = None
        regulus_client = getattr(bootstrap, "regulus_client", None)
        if regulus_client is not None:
            regulus_base_url = getattr(regulus_client, "base_url", None)

        # Run all checks concurrently.
        db_check, redis_check, regulus_check, schema_revision = await asyncio.gather(
            check_database(database) if database else _unavailable("database not configured"),
            check_redis(redis_url),
            check_regulus(regulus_base_url),
            check_schema_revision(database)
            if database
            else _unknown_schema_revision(),
        )

        checks = {
            "database": db_check,
            "redis": redis_check,
            "regulus": regulus_check,
        }
        compatibility = getattr(bootstrap, "langgraph_gateway_compatibility", None)
        if isinstance(compatibility, CompatibilityResult):
            checks["agent_server"] = DependencyStatus(
                status=compatibility.status.value,
                detail=compatibility.reason,
            )
        delivery = audit_delivery_health(bootstrap)
        if delivery is not None:
            checks["audit_delivery"] = _audit_delivery_check(delivery)

        status = determine_readiness_status(checks, schema_revision)
        return ReadinessResponse(
            status=status,
            checks=checks,
            schema_revision=schema_revision,
        )

    @app.get("/health/live", response_model=LivenessResponse)
    async def health_live() -> LivenessResponse:
        return LivenessResponse()


async def _unavailable(detail: str) -> DependencyStatus:
    """Return an unavailable dependency status."""
    return DependencyStatus(status="unavailable", detail=detail)


async def _unknown_schema_revision() -> SchemaRevision:
    """Return missing service evidence through the gather-compatible seam."""
    return unknown_schema_revision(SERVICE_MIGRATIONS_PACKAGE)


class LangGraphCompatibilityHealth(BaseModel):
    """Evidence from the one bounded Agent Server startup detection."""

    model_config = ConfigDict(extra="forbid")

    tested_langgraph: tuple[str, ...]
    tested_agent_server: tuple[str, ...]
    detected_agent_server: str | None
    status: str
    openapi_fingerprint: str | None


class LangGraphGatewayHealth(BaseModel):
    """Conservative deployment-level gateway capability surface."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    governance_level: GovernanceLevel
    limitation: str = "internal tool calls are not enforced in gateway-only mode"
    compatibility: LangGraphCompatibilityHealth


def langgraph_gateway_health(bootstrap: object) -> LangGraphGatewayHealth | None:
    """Build deployment gateway health only from bounded startup evidence."""
    compatibility = getattr(bootstrap, "langgraph_gateway_compatibility", None)
    if not isinstance(compatibility, CompatibilityResult):
        return None
    reporter = getattr(bootstrap, "langgraph_gateway_capability_reporter", None)
    level = GovernanceLevel.ADMISSION
    if reporter is not None:
        enforcement = getattr(bootstrap, "langgraph_enforcement_service", None)
        reported = reporter.report_deployment(
            getattr(enforcement, "deployment_evidence", None),
            graph_version=getattr(
                getattr(bootstrap, "deployment", None), "graph_version_ref", None
            ),
        )
        if isinstance(reported, GovernanceLevel):
            level = reported
    return LangGraphGatewayHealth(
        governance_level=level,
        compatibility=LangGraphCompatibilityHealth(
            tested_langgraph=compatibility.tested_langgraph_versions,
            tested_agent_server=compatibility.tested_agent_server_versions,
            detected_agent_server=compatibility.detected_agent_server_version,
            status=compatibility.status.value,
            openapi_fingerprint=compatibility.openapi_fingerprint,
        ),
    )


class AuditDeliveryHealth(BaseModel):
    """The bounded audit-delivery stage's own accounting, rendered as health.

    Read straight off the stage's counters rather than recomputed: "delivered"
    is the only counter that means an event is durable, and the three loss
    counters are reported beside it so a failing stage can never be read as a
    healthy one. ``queue_depth`` is the backlog still waiting for the worker.
    """

    model_config = ConfigDict(extra="forbid")

    queue_depth: int
    queued: int
    delivered: int
    retried: int
    rejected: int
    failed: int
    abandoned: int
    losing_events: bool


def audit_delivery_health(bootstrap: object) -> AuditDeliveryHealth | None:
    """Build audit-delivery health from the delivery stage's own counters.

    Args:
        bootstrap: The service container. ``None`` is returned when it owns no
            delivery stage, which is the case for every deployment that does
            not run the gateway.

    Returns:
        The stage's depth, its per-outcome counts and whether it has lost any
        event, or ``None`` when there is no stage to report on.
    """
    delivery = getattr(bootstrap, "audit_delivery_queue", None)
    if type(delivery) is not AuditDeliveryQueue:
        return None
    counts = delivery.counts()
    return AuditDeliveryHealth(
        queue_depth=delivery.pending,
        queued=counts.queued,
        delivered=counts.delivered,
        retried=counts.retried,
        rejected=counts.rejected,
        failed=counts.failed,
        abandoned=counts.abandoned,
        losing_events=bool(counts.rejected or counts.failed or counts.abandoned),
    )


def _audit_delivery_check(delivery: AuditDeliveryHealth) -> DependencyStatus:
    """Render audit-delivery health as one readiness dependency.

    The detail always carries the depth and the three loss counts, so an
    operator reading a bare probe sees what was lost and not merely that
    something was.
    """
    return DependencyStatus(
        status="error" if delivery.losing_events else "ok",
        detail=(
            f"queue_depth={delivery.queue_depth} delivered={delivery.delivered} "
            f"rejected={delivery.rejected} failed={delivery.failed} "
            f"abandoned={delivery.abandoned}"
        ),
    )


class HealthResponse(BaseModel):
    """Response payload for the service health endpoint."""

    model_config = ConfigDict(extra="forbid")

    status: str = Field(default="ok")
    deployment_ref: str
    deployment_version: int
    graph_version_ref: str
    langgraph_gateway: LangGraphGatewayHealth | None = None
    audit_delivery: AuditDeliveryHealth | None = None


_health_parameters = inspect.signature(HealthResponse).parameters
HealthResponse.__signature__ = inspect.signature(HealthResponse).replace(
    parameters=[
        parameter
        for name, parameter in _health_parameters.items()
        if name not in {"langgraph_gateway", "audit_delivery"}
    ]
)
