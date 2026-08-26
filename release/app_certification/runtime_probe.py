"""Certifier-owned probes for enabled runtime extras and mounted Regulus."""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

from .http_process import run_http_exchange

_PROBE_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,80}$")
_TENANT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def _regulus_origin(url: str) -> str:
    parsed = urlsplit(url.rstrip("/"))
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.endswith("/regulus/v1")
    ):
        raise ValueError("Regulus probe URL must be a local /regulus/v1 HTTP origin")
    return url.rstrip("/")


def _request(
    origin: str,
    path: str,
    *,
    method: str,
    body: object | None,
    headers: dict[str, str],
) -> object:
    status, payload = run_http_exchange(
        origin + path,
        method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json", **headers},
        body=body,
        timeout=10,
    )
    if status != 200:
        raise ValueError(f"Regulus probe {method} {path} returned HTTP {status}")
    return payload


def _require_identity(payload: object, field: str, expected: str, detail: str) -> None:
    if not isinstance(payload, dict) or payload.get(field) != expected:
        raise ValueError(detail)


def _register_probe(
    origin: str,
    tenant: str,
    capability_id: str,
    implementation_id: str,
    headers: dict[str, str],
) -> None:
    capability = _request(
        origin,
        "/capabilities",
        method="POST",
        body={"id": capability_id, "name": "App certification", "tenant_id": tenant},
        headers=headers,
    )
    _require_identity(
        capability,
        "id",
        capability_id,
        "Regulus capability probe returned the wrong identity",
    )
    implementation = _request(
        origin,
        f"/capabilities/{capability_id}/implementations",
        method="POST",
        body={"id": implementation_id, "name": "Certified runtime", "tenant_id": tenant},
        headers=headers,
    )
    _require_identity(
        implementation,
        "id",
        implementation_id,
        "Regulus implementation probe returned the wrong identity",
    )


def _ingest_probe_execution(
    origin: str,
    tenant: str,
    capability_id: str,
    implementation_id: str,
    execution_id: str,
    headers: dict[str, str],
) -> None:
    instrumentation = _request(
        origin,
        "/instrumentation/executions",
        method="POST",
        body={
            "capability_id": capability_id,
            "compute_cost_usd": "0.0",
            "compute_time_ms": 1,
            "execution_id": execution_id,
            "implementation_id": implementation_id,
            "latency_ms": 1,
            "metadata": {"tenant_id": tenant},
            "model_version": "certified",
            "tenant_id": tenant,
            "timestamp": datetime.now(UTC).isoformat(),
            "token_cost_usd": "0.001",
            "tool_cost_usd": "0.0",
        },
        headers=headers,
    )
    _require_identity(
        instrumentation,
        "execution_id",
        execution_id,
        "Regulus instrumentation probe did not persist the execution",
    )


def _set_budget(origin: str, tenant: str, tenant_path: str, headers: dict[str, str]) -> None:
    budget = _request(
        origin,
        f"/budget/tenants/{tenant_path}",
        method="PUT",
        body={"budget_cap_usd": 1.0},
        headers=headers,
    )
    _require_identity(budget, "tenant_id", tenant, "Regulus budget probe returned the wrong tenant")


def _check_budget(origin: str, tenant: str, tenant_path: str, headers: dict[str, str]) -> None:
    status = _request(
        origin,
        f"/budget/status?tenant_id={tenant_path}",
        method="GET",
        body=None,
        headers=headers,
    )
    if (
        not isinstance(status, dict)
        or status.get("tenant_id") != tenant
        or float(status.get("total_cost_usd", 0)) < 0.001
        or status.get("budget_cap_usd") != 1.0
    ):
        raise ValueError("Regulus budget status did not include the instrumented cost")


def probe_regulus(url: str, tenant: str, probe_id: str, mode: str) -> None:
    """Prove authenticated capability, budget, and instrumentation operations."""
    if (
        mode not in {"packaged", "ephemeral"}
        or _PROBE_ID.fullmatch(probe_id) is None
        or _TENANT_ID.fullmatch(tenant) is None
    ):
        raise ValueError("Regulus probe mode, ID, or tenant is invalid")
    api_key = os.environ.get("APP_CERTIFICATION_API_KEY")
    token = os.environ.get("APP_CERTIFICATION_REGULUS_TOKEN")
    if not api_key or not token:
        raise ValueError("Regulus probe requires certifier-owned API and service credentials")
    origin = _regulus_origin(url)
    tenant_path = quote(tenant, safe="")
    headers = {"Authorization": f"Bearer {token}", "X-API-Key": api_key}
    capability_id = f"app-cert-{probe_id}-{mode}"
    implementation_id = f"{capability_id}-runtime"
    _register_probe(origin, tenant, capability_id, implementation_id, headers)
    _set_budget(origin, tenant, tenant_path, headers)
    execution_id = f"exec-{probe_id}-{mode}"
    _ingest_probe_execution(
        origin,
        tenant,
        capability_id,
        implementation_id,
        execution_id,
        headers,
    )
    _check_budget(origin, tenant, tenant_path, headers)
