"""Canonical import surface for the service process entry point."""

from __future__ import annotations


def test_entrypoint_publishes_its_whole_surface() -> None:
    """Every name the package is documented to export is still exported.

    This replaced a parity assertion comparing each name against the legacy
    republisher. ZER-25 removed that path, so the comparison would compare
    the module with itself; the surface it pinned is asserted directly.
    """
    import zeroth.service.entrypoint as canonical

    expected = {
        "app_factory",
        "main",
    }

    missing = sorted(name for name in expected if not hasattr(canonical, name))
    assert not missing, f"zeroth.service.entrypoint no longer publishes: {missing}"
