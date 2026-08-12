"""Structural discovery of production tenant-scoped service repositories."""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterable
from types import ModuleType

import zeroth.contracts.graph as contracts_graph
import zeroth.contracts.registry as contracts_registry
import zeroth.governance.approvals as governance_approvals
import zeroth.governance.attestations as governance_attestations
import zeroth.governance.audit as governance_audit
import zeroth.governance.decisions as governance_decisions
import zeroth.governance.guardrails as governance_guardrails
import zeroth.governance.retention as governance_retention
import zeroth.integrations.memory as integrations_memory
import zeroth.integrations.persistence.runs as persistence_runs
import zeroth.platform.dispatch as platform_dispatch
import zeroth.service.deployments as service_deployments
import zeroth.service.langgraph_gateway as langgraph_gateway
import zeroth.service.webhooks as service_webhooks
from zeroth.platform.storage.scoping import (
    PersistenceProbe,
    PersistenceSurface,
    ResourceOperation,
    discover_persistence_surfaces,
)

_PERSISTENCE_ROOTS: tuple[ModuleType, ...] = (
    contracts_graph,
    contracts_registry,
    governance_approvals,
    governance_attestations,
    governance_audit,
    governance_decisions,
    governance_guardrails,
    governance_retention,
    integrations_memory,
    persistence_runs,
    platform_dispatch,
    service_deployments,
    langgraph_gateway,
    service_webhooks,
)


def _import_persistence_packages() -> None:
    for package in _PERSISTENCE_ROOTS:
        paths = getattr(package, "__path__", None)
        if paths is None:
            continue
        module_names = sorted(
            item.name for item in pkgutil.walk_packages(paths, prefix=f"{package.__name__}.")
        )
        for module_name in module_names:
            importlib.import_module(module_name)


def load_service_persistence_surfaces() -> tuple[PersistenceSurface, ...]:
    """Import scoped persistence roots and discover decorated repositories."""
    _import_persistence_packages()
    return tuple(
        surface
        for surface in discover_persistence_surfaces()
        if surface.resource_name.startswith("service.")
    )


def surface_operation_pairs(
    surfaces: Iterable[PersistenceSurface],
) -> frozenset[tuple[str, ResourceOperation]]:
    """Derive case pairs from decorated production methods."""
    return frozenset(
        (surface.resource_name, operation)
        for surface in surfaces
        for operations in surface.operation_methods.values()
        for operation in operations
    )


def executable_probe_for(
    surfaces: Iterable[PersistenceSurface],
    resource_name: str,
    operation: ResourceOperation,
) -> PersistenceProbe:
    """Resolve exactly one production-owned executable probe for a case."""
    matches = [
        surface.probe
        for surface in surfaces
        if surface.resource_name == resource_name
        and surface.probe is not None
        and operation
        in {
            candidate
            for operations in surface.operation_methods.values()
            for candidate in operations
        }
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected exactly one executable probe for {resource_name}:{operation.value}; "
            f"found {len(matches)}"
        )
    return matches[0]


__all__ = [
    "executable_probe_for",
    "load_service_persistence_surfaces",
    "surface_operation_pairs",
]
