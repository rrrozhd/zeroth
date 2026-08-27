"""Executable-unit manifest listing REST API.

Surfaces the executable-unit registry and agent runners populated at bootstrap
so operator UIs — the console's executable-unit-node inspector in particular —
can offer the actual resolvable ``manifest_ref`` values instead of a blind
free-text field.
"""

from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import unquote

from fastapi import APIRouter, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from zeroth.governance.audit import AuditRepository
from zeroth.governance.audit.models import AuditQuery
from zeroth.integrations.execution.integrity import compute_manifest_digest
from zeroth.service.api.authorization import Permission, require_permission


class ManifestSummaryResponse(BaseModel):
    """One registered executable unit or agent runner, as shown in the console."""

    model_config = ConfigDict(extra="forbid")

    manifest_ref: str
    kind: str
    runtime: str | None = None
    description: str | None = None


class ManifestDetailResponse(ManifestSummaryResponse):
    """Secret-free executable-unit configuration suitable for operator inspection."""

    version: int | None = None
    onboarding_mode: str | None = None
    artifact_source_kind: str | None = None
    entrypoint_type: str | None = None
    input_mode: str | None = None
    output_mode: str | None = None
    input_contract_ref: str | None = None
    output_contract_ref: str | None = None
    capability_requests: list[str] = Field(default_factory=list)
    resource_limits: dict[str, Any] | None = None
    timeout_seconds: int | None = None
    execution_placement: str | None = None
    side_effect: bool | None = None
    content_hash: str | None = None
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None


class ManifestRunLinkResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    node_id: str
    status: str


class ManifestRunListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest_ref: str
    runs: list[ManifestRunLinkResponse] = Field(default_factory=list)


def _orchestrator(request: Request) -> Any | None:
    return getattr(request.app.state.bootstrap, "orchestrator", None)


def _binding(request: Request, manifest_ref: str) -> Any | None:
    orchestrator = _orchestrator(request)
    runner = getattr(orchestrator, "executable_unit_runner", None)
    registry = getattr(runner, "registry", None)
    if registry is None:
        return None
    resolved = unquote(manifest_ref)
    return registry.get(resolved) if registry.has(resolved) else None


def _enum_value(value: Any) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value))


def register_manifest_routes(app: FastAPI | APIRouter) -> None:
    """Register executable-unit manifest listing routes on the FastAPI app."""

    @app.get("/manifests", response_model=list[ManifestSummaryResponse])
    async def list_manifests(request: Request) -> list[ManifestSummaryResponse]:
        """All executable units and agent runners registered for this deployment.

        These are the values an executable_unit node's ``manifest_ref`` can
        reference, plus the runner names agent nodes bind to at run time.
        """
        await require_permission(request, Permission.DEPLOYMENT_READ)
        orchestrator = _orchestrator(request)
        if orchestrator is None:
            return []
        entries: list[ManifestSummaryResponse] = []
        runner = getattr(orchestrator, "executable_unit_runner", None)
        registry = getattr(runner, "registry", None)
        if registry is not None:
            for ref, binding in registry.list().items():
                manifest = binding.manifest
                runtime = getattr(manifest, "runtime", None)
                entries.append(
                    ManifestSummaryResponse(
                        manifest_ref=ref,
                        kind="executable_unit",
                        runtime=(
                            str(getattr(runtime, "value", runtime)) if runtime is not None else None
                        ),
                        description=(
                            getattr(manifest, "description", None)
                            or binding.metadata.get("description")
                        ),
                    )
                )
        entries.extend(
            ManifestSummaryResponse(manifest_ref=name, kind="agent_runner")
            for name in getattr(orchestrator, "agent_runners", None) or {}
        )
        return sorted(entries, key=lambda m: (m.kind, m.manifest_ref))

    @app.get(
        "/manifests/{manifest_ref:path}/runs",
        response_model=ManifestRunListResponse,
    )
    async def list_manifest_runs(
        request: Request,
        manifest_ref: str,
    ) -> ManifestRunListResponse:
        """Recent run/node identities linked to a manifest through scoped audit evidence."""
        principal = await require_permission(request, Permission.AUDIT_READ)
        binding = _binding(request, manifest_ref)
        if binding is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="manifest not found")
        deployment = request.app.state.bootstrap.deployment
        audit_repository: AuditRepository = request.app.state.bootstrap.audit_repository
        records = await audit_repository.list(
            AuditQuery(
                deployment_ref=deployment.deployment_ref,
                tenant_id=principal.tenant_id,
                workspace_id=deployment.workspace_id,
                workspace_scoped=True,
            ),
            limit=500,
        )
        resolved = unquote(manifest_ref)
        resolved_digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()
        linked: list[ManifestRunLinkResponse] = []
        seen: set[tuple[str, str]] = set()
        for record in reversed(records):
            if record.execution_metadata.get("manifest_ref_sha256") != resolved_digest:
                continue
            identity = (record.run_id, record.node_id)
            if identity in seen:
                continue
            seen.add(identity)
            linked.append(
                ManifestRunLinkResponse(
                    run_id=record.run_id,
                    node_id=record.node_id,
                    status=record.status,
                )
            )
            if len(linked) >= 10:
                break
        return ManifestRunListResponse(manifest_ref=resolved, runs=linked)

    @app.get(
        "/manifests/{manifest_ref:path}",
        response_model=ManifestDetailResponse,
    )
    async def get_manifest(request: Request, manifest_ref: str) -> ManifestDetailResponse:
        """Return a safe manifest projection; commands, source and environment stay hidden."""
        await require_permission(request, Permission.DEPLOYMENT_READ)
        binding = _binding(request, manifest_ref)
        if binding is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="manifest not found")
        manifest = binding.manifest
        return ManifestDetailResponse(
            manifest_ref=binding.manifest_ref,
            kind="executable_unit",
            runtime=_enum_value(manifest.runtime),
            description=getattr(manifest, "description", None)
            or binding.metadata.get("description"),
            version=manifest.version,
            onboarding_mode=_enum_value(manifest.onboarding_mode),
            artifact_source_kind=getattr(manifest.artifact_source, "kind", None),
            entrypoint_type=_enum_value(manifest.entrypoint_type),
            input_mode=_enum_value(manifest.input_mode),
            output_mode=_enum_value(manifest.output_mode),
            input_contract_ref=manifest.input_contract_ref,
            output_contract_ref=manifest.output_contract_ref,
            capability_requests=list(manifest.capability_requests),
            resource_limits=manifest.resource_limits.model_dump(mode="json"),
            timeout_seconds=manifest.timeout_seconds,
            execution_placement=_enum_value(manifest.execution_placement),
            side_effect=manifest.side_effect,
            content_hash=compute_manifest_digest(manifest),
            input_schema=binding.input_model.model_json_schema(),
            output_schema=binding.output_model.model_json_schema(),
        )
