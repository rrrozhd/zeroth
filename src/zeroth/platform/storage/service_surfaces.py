"""Structural discovery of production tenant-scoped service repositories."""

from __future__ import annotations

import ast
import importlib
import pkgutil
from collections.abc import Iterable

import zeroth
from zeroth.platform.storage.scoping import (
    PersistenceProbe,
    PersistenceSurface,
    ResourceOperation,
    discover_persistence_surfaces,
)


def _import_persistence_packages() -> None:
    modules = sorted(
        pkgutil.walk_packages(zeroth.__path__, prefix="zeroth."),
        key=lambda item: item.name,
    )
    for module in modules:
        spec = module.module_finder.find_spec(module.name)
        loader = None if spec is None else spec.loader
        get_source = getattr(loader, "get_source", None)
        source = get_source(module.name) if get_source is not None else None
        if source is not None and "persistence_surface(" in source:
            package_name = module.name.rpartition(".")[0]
            dependencies = sorted(
                node.module
                for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.ImportFrom)
                and node.module is not None
                and node.module.startswith(f"{package_name}.")
            )
            for dependency in dependencies:
                importlib.import_module(dependency)
            importlib.import_module(module.name)


def load_service_persistence_surfaces() -> tuple[PersistenceSurface, ...]:
    """Import decorated repositories beneath ``zeroth`` and discover their surfaces."""
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
