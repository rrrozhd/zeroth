"""Legacy import path for the Agent Server-compatible LangGraph gateway.

Every module this package once held now lives in a canonical package -- the data
shapes in :mod:`zeroth.contracts.langgraph_gateway`, the governance surfaces in
:mod:`zeroth.governance.langgraph_gateway`, the request-time machinery in
:mod:`zeroth.service.langgraph_gateway`, and the adapter wire protocol in
:mod:`zeroth.integrations.langgraph.enforcement_protocol`. Import from those
instead (see docs/backend-import-migration.md).

Resolution is lazy, and that is the whole point of this file rather than a
convenience. Python imports a package before any of its submodules, so an eager
re-export here would load the contracts package the moment anyone touched a
legacy shim -- which would defeat the laziness of every other shim in this
directory and put the canonical tree back on the import path this relocation
exists to clear.

The export list is the surface captured before the move, in
``tests/langgraph_gateway/fixtures/legacy_surface_manifest.json``. The package
declared ``__all__`` before, so it still does; ``__dir__`` is overridden so the
surface survives lazy resolution.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from zeroth.contracts.langgraph_gateway.inventory import (
        ENDPOINT_RULES,
        EndpointRule,
        classify_endpoint,
        classify_protocol_command,
    )
    from zeroth.contracts.langgraph_gateway.models import (
        AdmissionDecision,
        AdmissionRequest,
        CompatibilityResult,
        CompatibilityStatus,
        EndpointKind,
        GatewayCorrelation,
        GatewayError,
        GatewayEvent,
        GatewayEventStatus,
        GovernanceLevel,
        RouteDisposition,
        RunCapabilityEvidence,
    )

# Each legacy name and the canonical contracts module that now owns it.
_EXPORTS = {
    "AdmissionDecision": "models",
    "AdmissionRequest": "models",
    "CompatibilityResult": "models",
    "CompatibilityStatus": "models",
    "ENDPOINT_RULES": "inventory",
    "EndpointKind": "models",
    "EndpointRule": "inventory",
    "GatewayCorrelation": "models",
    "GatewayError": "models",
    "GatewayEvent": "models",
    "GatewayEventStatus": "models",
    "GovernanceLevel": "models",
    "RouteDisposition": "models",
    "RunCapabilityEvidence": "models",
    "classify_endpoint": "inventory",
    "classify_protocol_command": "inventory",
}

__all__ = [
    "AdmissionDecision",
    "AdmissionRequest",
    "CompatibilityResult",
    "CompatibilityStatus",
    "ENDPOINT_RULES",
    "EndpointKind",
    "EndpointRule",
    "GatewayCorrelation",
    "GatewayError",
    "GatewayEvent",
    "GatewayEventStatus",
    "GovernanceLevel",
    "RouteDisposition",
    "RunCapabilityEvidence",
    "classify_endpoint",
    "classify_protocol_command",
]


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    import importlib

    canonical = importlib.import_module(f"zeroth.contracts.langgraph_gateway.{module}")
    return getattr(canonical, name)


def __dir__() -> list[str]:
    return sorted(_EXPORTS)
