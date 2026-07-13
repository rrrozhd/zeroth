"""Executable-unit manifest listing REST API.

Surfaces the executable-unit registry and agent runners populated at bootstrap
so operator UIs — the console's executable-unit-node inspector in particular —
can offer the actual resolvable ``manifest_ref`` values instead of a blind
free-text field.
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, Request
from pydantic import BaseModel, ConfigDict

from zeroth.core.service.authorization import Permission, require_permission


class ManifestSummaryResponse(BaseModel):
    """One registered executable unit or agent runner, as shown in the console."""

    model_config = ConfigDict(extra="forbid")

    manifest_ref: str
    kind: str
    runtime: str | None = None
    description: str | None = None


def register_manifest_routes(app: FastAPI | APIRouter) -> None:
    """Register executable-unit manifest listing routes on the FastAPI app."""

    @app.get("/manifests", response_model=list[ManifestSummaryResponse])
    async def list_manifests(request: Request) -> list[ManifestSummaryResponse]:
        """All executable units and agent runners registered for this deployment.

        These are the values an executable_unit node's ``manifest_ref`` can
        reference, plus the runner names agent nodes bind to at run time.
        """
        await require_permission(request, Permission.DEPLOYMENT_READ)
        orchestrator = getattr(request.app.state.bootstrap, "orchestrator", None)
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
