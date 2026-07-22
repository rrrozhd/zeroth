"""Canonical import surface for the service health module."""

from __future__ import annotations


def test_health_is_the_same_surface_through_both_paths() -> None:
    from zeroth.core.service import health as legacy
    from zeroth.service.api import health as canonical

    assert canonical.register_health_routes is legacy.register_health_routes
    assert canonical.LivenessResponse is legacy.LivenessResponse
    assert canonical.ReadinessResponse is legacy.ReadinessResponse
    assert canonical.DependencyStatus is legacy.DependencyStatus


def test_wrapper_health_response_moved_next_to_the_health_routes() -> None:
    """HealthResponse relocates out of app.py deliberately.

    ``_discover_schema_models`` treats any ``app.py`` whose parent directory is
    literally ``service`` as schema-bearing. ``zeroth/service/app.py`` — the
    app module's destination — matches that predicate, so a model defined
    there would be discovered under its new module path while the canonical
    fixture still records the old one, deadlocking the app move against the
    golden-fixture rule. Defining it beside the other health schemas keeps it
    out of that trap; the legacy ``zeroth.core.service.app`` path still
    resolves the same class object.
    """
    from zeroth.core.service.app import HealthResponse as LegacyHealthResponse
    from zeroth.service.api.health import HealthResponse

    assert HealthResponse is LegacyHealthResponse
    assert HealthResponse.__module__ == "zeroth.service.api.health"
