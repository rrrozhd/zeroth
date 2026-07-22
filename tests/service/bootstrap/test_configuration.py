"""Canonical import surface for the extracted service bootstrap configuration."""

from __future__ import annotations


def test_bootstrap_memory_defaults_disable_every_external_backend() -> None:
    from zeroth.service.bootstrap.configuration import _BootstrapMemorySettings

    settings = _BootstrapMemorySettings()
    assert settings.memory.default_connector == "ephemeral"
    assert settings.pgvector.enabled is False
    assert settings.chroma.enabled is False
    assert settings.elasticsearch.enabled is False


def test_run_migrations_is_the_same_object_through_both_paths() -> None:
    from zeroth.core.service.bootstrap import run_migrations as legacy
    from zeroth.service.bootstrap.migrations import run_migrations as canonical

    assert canonical is legacy
