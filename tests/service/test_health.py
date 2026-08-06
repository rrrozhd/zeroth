"""Canonical import surface for the service health module."""

from __future__ import annotations


def test_health_publishes_its_whole_surface() -> None:
    """Every name the package is documented to export is still exported.

    This replaced a parity assertion comparing each name against the legacy
    republisher. ZER-25 removed that path, so the comparison would compare
    the module with itself; the surface it pinned is asserted directly.
    """
    import zeroth.service.api.health as canonical

    expected = {
        "DependencyStatus",
        "LivenessResponse",
        "ReadinessResponse",
        "register_health_routes",
    }

    missing = sorted(name for name in expected if not hasattr(canonical, name))
    assert not missing, f"zeroth.service.api.health no longer publishes: {missing}"


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
