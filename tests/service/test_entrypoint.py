"""Canonical import surface for the service process entry point."""

from __future__ import annotations


def test_entrypoint_is_the_same_surface_through_both_paths() -> None:
    from zeroth.core.service import entrypoint as legacy
    from zeroth.service import entrypoint as canonical

    assert canonical.main is legacy.main
    assert canonical.app_factory is legacy.app_factory
    assert callable(canonical.main)
