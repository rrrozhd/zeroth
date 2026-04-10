"""Health probe endpoints for container orchestration.

Provides readiness and liveness probes with per-dependency status
checking for database, Redis, and Regulus.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import httpx
from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import from_url as redis_from_url

if TYPE_CHECKING:
    from fastapi import FastAPI

    from zeroth.core.storage.database import AsyncDatabase


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


class LivenessResponse(BaseModel):
    """Response payload for the liveness probe."""

    model_config = ConfigDict(extra="forbid")

    status: str = "ok"


def determine_readiness_status(checks: dict[str, DependencyStatus]) -> str:
    """Determine overall readiness status from per-dependency checks.

    Rules:
    - If database or redis has status != "ok" -> "unhealthy"
    - If only regulus has status != "ok" -> "degraded"
    - Otherwise -> "ok"
    """
    db_status = checks.get("database")
    redis_status = checks.get("redis")
    regulus_status = checks.get("regulus")

    if (db_status and db_status.status != "ok") or (
        redis_status and redis_status.status != "ok"
    ):
        return "unhealthy"

    if regulus_status and regulus_status.status != "ok":
        return "degraded"

    return "ok"


async def check_database(db: AsyncDatabase) -> DependencyStatus:
    """Check database connectivity by executing SELECT 1."""
    start = time.monotonic()
    try:
        async with db.transaction() as conn:
            await conn.fetch_one("SELECT 1")
        elapsed_ms = (time.monotonic() - start) * 1000
        return DependencyStatus(status="ok", latency_ms=elapsed_ms)
    except Exception as exc:
        elapsed_ms = (time.monotonic() - start) * 1000
        return DependencyStatus(
            status="error", latency_ms=elapsed_ms, detail=str(exc)
        )


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
            status="error", latency_ms=elapsed_ms, detail=str(exc)
        )


async def check_regulus(
    base_url: str | None, timeout: float = 5.0
) -> DependencyStatus:
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
    from fastapi import Request

    @app.get("/health/ready", response_model=ReadinessResponse)
    async def health_ready(request: Request) -> ReadinessResponse:
        bootstrap = request.app.state.bootstrap

        # Gather database reference from bootstrap.
        database = getattr(bootstrap, "database", None)

        # Build Redis URL from settings if available.
        redis_url: str | None = None
        try:
            from zeroth.core.config.settings import get_settings

            settings = get_settings()
            rs = settings.redis
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
        db_check, redis_check, regulus_check = await asyncio.gather(
            check_database(database) if database else _unavailable("database not configured"),
            check_redis(redis_url),
            check_regulus(regulus_base_url),
        )

        checks = {
            "database": db_check,
            "redis": redis_check,
            "regulus": regulus_check,
        }

        status = determine_readiness_status(checks)
        return ReadinessResponse(status=status, checks=checks)

    @app.get("/health/live", response_model=LivenessResponse)
    async def health_live() -> LivenessResponse:
        return LivenessResponse()


async def _unavailable(detail: str) -> DependencyStatus:
    """Return an unavailable dependency status."""
    return DependencyStatus(status="unavailable", detail=detail)
