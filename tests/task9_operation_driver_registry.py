"""Compatibility accessors for production-owned isolation probes."""

from __future__ import annotations

from functools import partial
from types import MappingProxyType

from zeroth.platform.storage import ResourceOperation
from zeroth.platform.storage.service_surfaces import (
    executable_probe_for,
    load_service_persistence_surfaces,
    surface_operation_pairs,
)

_DISCOVERED_SURFACES = load_service_persistence_surfaces()


def semantic_driver_for(resource_name: str, operation: ResourceOperation):
    """Resolve a production probe without maintaining a test-side case inventory."""
    try:
        probe = executable_probe_for(_DISCOVERED_SURFACES, resource_name, operation)
    except AssertionError:
        return None
    return partial(probe, operation=operation)


TASK9_EXECUTABLE_DRIVERS = MappingProxyType(
    {
        pair: partial(executable_probe_for(_DISCOVERED_SURFACES, *pair), operation=pair[1])
        for pair in surface_operation_pairs(_DISCOVERED_SURFACES)
    }
)


__all__ = ["TASK9_EXECUTABLE_DRIVERS", "semantic_driver_for"]
