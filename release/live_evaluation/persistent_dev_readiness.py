"""Provider-free discovery for the persistent local evaluation instance."""

from __future__ import annotations

import json
import sqlite3
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import yaml

from .campaign_execution import CampaignExecutionSettings, build_campaign_execution
from .config import CampaignConfig


@dataclass(frozen=True, slots=True)
class EndpointObservation:
    status_code: int
    payload: Mapping[str, object] | None


@dataclass(frozen=True, slots=True)
class SecretReference:
    """Opaque location only; discovery never reads the referenced value."""

    name: str
    reference: str
    present: bool


@dataclass(frozen=True, slots=True)
class PersistentDevRuntimeWiring:
    campaign_id: str
    tenant_id: str
    service_database: Path
    econ_database: Path
    action_sink_database: Path
    provider_window: Path
    console_origin: str
    twin_origin: str
    frontend_origin: str
    chroma_origin: str
    active_deployment_ref: str | None
    campaign_deployment_refs: tuple[str, ...]
    audit_endpoints: Mapping[str, str]
    audit_readiness_endpoint: str
    campaign_run_ids: tuple[str, ...]
    secret_references: tuple[SecretReference, ...]
    compose_status: tuple[str, ...]
    backend_restart: tuple[str, ...]
    backend_rebuild: tuple[str, ...]
    frontend_restart: tuple[str, ...]
    blockers: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.blockers

    def require_ready(self) -> PersistentDevRuntimeWiring:
        if self.blockers:
            raise RuntimeError("persistent dev runtime blocked: " + ", ".join(self.blockers))
        return self


def _origin(service: Mapping[str, object], *, name: str) -> str:
    ports = service.get("ports")
    if not isinstance(ports, list) or len(ports) != 1 or not isinstance(ports[0], str):
        raise ValueError(f"{name} must publish one explicit loopback port")
    parts = ports[0].split(":")
    if len(parts) != 3 or parts[0] != "127.0.0.1":
        raise ValueError(f"{name} must publish one explicit loopback port")
    try:
        host_port = int(parts[1])
        container_port = int(parts[2])
    except ValueError as exc:
        raise ValueError(f"{name} has an invalid port mapping") from exc
    if not (1 <= host_port <= 65535 and 1 <= container_port <= 65535):
        raise ValueError(f"{name} has an invalid port mapping")
    return f"http://127.0.0.1:{host_port}"


def _probe(url: str) -> EndpointObservation:
    try:
        with urlopen(url, timeout=2) as response:  # noqa: S310 - explicit loopback URLs only
            body = response.read()
            try:
                payload = json.loads(body) if body else None
            except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
                payload = None
            return EndpointObservation(
                status_code=int(response.status),
                payload=payload if isinstance(payload, dict) else None,
            )
    except (OSError, URLError, ValueError, json.JSONDecodeError):
        return EndpointObservation(status_code=0, payload=None)


def _secret_reference(name: str, reference: str, path: Path | None) -> SecretReference:
    present = True
    if path is not None:
        try:
            metadata = path.stat()
            present = (
                path.is_file()
                and metadata.st_size > 0
                and not bool(metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO))
            )
        except OSError:
            present = False
    return SecretReference(name=name, reference=reference, present=present)


def _campaign_runs(database: Path, *, tenant_id: str, campaign_id: str) -> tuple[str, ...]:
    if not database.is_file():
        return ()
    try:
        with sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True) as connection:
            rows = connection.execute(
                """SELECT run_id FROM runs
                WHERE tenant_id = ? AND json_extract(metadata, '$.campaign_id') = ?
                ORDER BY started_at, run_id""",
                (tenant_id, campaign_id),
            ).fetchall()
    except sqlite3.DatabaseError:
        return ()
    values = tuple(str(row[0]) for row in rows if isinstance(row[0], str) and row[0])
    return values if len(values) == len(set(values)) else ()


def _provider_window_valid(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        total = Decimal(str(payload["total_usd"]))
        return (
            isinstance(payload, dict)
            and set(payload) == {"window_id", "total_usd"}
            and isinstance(payload["window_id"], str)
            and bool(payload["window_id"])
            and total.is_finite()
            and total >= 0
        )
    except (OSError, KeyError, TypeError, ValueError, InvalidOperation, json.JSONDecodeError):
        return False


def discover_persistent_dev_runtime(
    *,
    repository_root: Path,
    state_root: Path,
    campaign_config: Path,
    compose_file: Path,
    market_env: Path,
    endpoint_observations: Mapping[str, EndpointObservation] | None = None,
) -> PersistentDevRuntimeWiring:
    """Discover paths and public health only; no credential or provider operation occurs."""
    repository_root = repository_root.resolve(strict=True)
    state_root = state_root.resolve(strict=True)
    if state_root == repository_root or repository_root in state_root.parents:
        raise ValueError("persistent state must remain outside the repository")
    campaign = CampaignConfig.model_validate(
        json.loads(campaign_config.read_text(encoding="utf-8"))
    )
    if campaign.artifact_root.resolve() != state_root:
        raise ValueError("campaign artifact root does not match persistent state root")
    if campaign.action_sink_root.resolve() != state_root / "action-sink":
        raise ValueError("campaign action sink does not match persistent state root")
    compose = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    services = compose.get("services") if isinstance(compose, dict) else None
    if not isinstance(services, dict):
        raise ValueError("development compose services are missing")
    origins = {
        name: _origin(services[name], name=name)
        for name in ("backend", "backend-twin", "frontend", "chroma")
        if isinstance(services.get(name), dict)
    }
    if set(origins) != {"backend", "backend-twin", "frontend", "chroma"}:
        raise ValueError("development compose topology is incomplete")

    topology = build_campaign_execution(
        CampaignExecutionSettings(
            campaign_id=campaign.campaign_id,
            tenant_id=campaign.tenant_id,
            model=campaign.model,
            embedding_model=campaign.embedding_model,
            chroma_connector_ref="chroma",
        )
    ).deployments
    deployment_refs = (
        topology.workflow1,
        topology.workflow2_child,
        topology.workflow2_parent,
        topology.workflow3,
    )
    health_urls = {
        "backend": f"{origins['backend']}/health",
        "twin": f"{origins['backend-twin']}/health",
        "chroma": f"{origins['chroma']}/api/v2/heartbeat",
        "frontend": f"{origins['frontend']}/console/",
    }
    observations = (
        {url: _probe(url) for url in health_urls.values()}
        if endpoint_observations is None
        else dict(endpoint_observations)
    )
    blockers: list[str] = []
    backend = observations.get(health_urls["backend"], EndpointObservation(0, None))
    backend_payload = backend.payload or {}
    active_deployment = (
        backend_payload.get("deployment_ref")
        if isinstance(backend_payload.get("deployment_ref"), str)
        else None
    )
    if backend.status_code != 200 or backend_payload.get("status") != "ok":
        blockers.append("primary_backend_unavailable")
    else:
        if backend_payload.get("campaign_id") != campaign.campaign_id:
            blockers.append("active_campaign_identity_mismatch")
        if active_deployment not in deployment_refs:
            blockers.append("active_deployment_not_campaign_topology")
    if observations.get(health_urls["twin"], EndpointObservation(0, None)).status_code != 200:
        blockers.append("twin_backend_unavailable")
    if observations.get(health_urls["chroma"], EndpointObservation(0, None)).status_code != 200:
        blockers.append("chroma_unavailable")
    if observations.get(health_urls["frontend"], EndpointObservation(0, None)).status_code != 200:
        blockers.append("frontend_unavailable")

    service_database = state_root / "zeroth.db"
    econ_database = state_root / "econ.db"
    action_sink_database = state_root / "action-sink" / "actions.sqlite3"
    provider_window = state_root / "reconciliation" / f"{campaign.campaign_id}.provider-window.json"
    for path, code in (
        (service_database, "service_database_missing"),
        (econ_database, "econ_database_missing"),
        (action_sink_database, "action_sink_database_missing"),
    ):
        if not path.is_file():
            blockers.append(code)
    if not _provider_window_valid(provider_window):
        blockers.append(
            "provider_window_missing" if not provider_window.exists() else "provider_window_invalid"
        )
    run_ids = _campaign_runs(
        service_database,
        tenant_id=campaign.tenant_id,
        campaign_id=campaign.campaign_id,
    )
    if not run_ids:
        blockers.append("campaign_run_inventory_empty")

    secret_root = state_root / "runtime-secrets"
    references = (
        _secret_reference("provider_catalog", campaign.provider_secret_ref, None),
        _secret_reference("provider_environment_file", str(market_env.resolve()), market_env),
        _secret_reference(
            "service_api_key",
            str(secret_root / "service-api-key"),
            secret_root / "service-api-key",
        ),
        _secret_reference(
            "fault_controller_key",
            str(secret_root / "fault-controller-key"),
            secret_root / "fault-controller-key",
        ),
        _secret_reference(
            "signing_key", str(secret_root / "signing-key"), secret_root / "signing-key"
        ),
    )
    if any(not reference.present for reference in references):
        blockers.append("required_secret_reference_unavailable")

    compose_prefix = ("docker", "compose", "-f", str(compose_file.resolve()))
    return PersistentDevRuntimeWiring(
        campaign_id=campaign.campaign_id,
        tenant_id=campaign.tenant_id,
        service_database=service_database,
        econ_database=econ_database,
        action_sink_database=action_sink_database,
        provider_window=provider_window,
        console_origin=origins["backend"],
        twin_origin=origins["backend-twin"],
        frontend_origin=origins["frontend"],
        chroma_origin=origins["chroma"],
        active_deployment_ref=active_deployment,
        campaign_deployment_refs=deployment_refs,
        audit_endpoints={
            reference: f"{origins['backend']}/v1/deployments/{reference}/audits"
            for reference in deployment_refs
        },
        audit_readiness_endpoint=f"{origins['backend']}/v1/audit-readiness",
        campaign_run_ids=run_ids,
        secret_references=references,
        compose_status=(*compose_prefix, "ps"),
        backend_restart=(*compose_prefix, "restart", "backend"),
        backend_rebuild=(*compose_prefix, "up", "-d", "--build", "backend"),
        frontend_restart=(*compose_prefix, "restart", "frontend"),
        blockers=tuple(dict.fromkeys(blockers)),
    )
