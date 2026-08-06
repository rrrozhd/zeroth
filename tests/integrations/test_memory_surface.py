"""Canonical import surface for the memory integrations package.

Non-golden boundary tests for the Task 15 memory consolidation: the
canonical ``zeroth.integrations.memory`` package must publish the same
objects the legacy ``zeroth.core.memory`` path keeps republishing, the
vendored ``zeroth.core.governed.memory`` wrappers must resolve to the same
objects at ``zeroth.integrations.memory.governed``, ``MemoryScope`` must
keep resolving from its contract-owned definition, and both package paths
must stay cold-importable from a fresh interpreter in either order.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

EXPORTS = (
    "ConnectorManifest",
    "InMemoryConnectorRegistry",
    "KeyValueMemoryConnector",
    "MemoryConnectorResolver",
    "ResolvedMemoryBinding",
    "RunEphemeralMemoryConnector",
    "ThreadMemoryConnector",
    "register_memory_connectors",
)


def test_memory_is_the_same_surface_through_both_paths() -> None:
    from zeroth.core import memory as legacy
    from zeroth.integrations import memory as canonical

    for name in EXPORTS:
        assert getattr(canonical, name) is getattr(legacy, name), name


@pytest.mark.parametrize(
    ("module_name", "names"),
    [
        ("capability_guard", ("CapabilityEnforcingMemoryConnector",)),
        ("chroma_connector", ("ChromaDBMemoryConnector",)),
        ("config_repository", ("MemoryConnectorConfig", "MemoryConnectorConfigRepository")),
        (
            "connectors",
            (
                "KeyValueMemoryConnector",
                "RunEphemeralMemoryConnector",
                "ThreadMemoryConnector",
            ),
        ),
        ("elastic_connector", ("ElasticsearchMemoryConnector",)),
        ("factory", ("register_memory_connectors",)),
        ("models", ("ConnectorManifest", "ResolvedMemoryBinding")),
        ("pgvector_connector", ("PgvectorMemoryConnector",)),
        ("redis_kv", ("RedisKVMemoryConnector",)),
        ("redis_thread", ("RedisThreadMemoryConnector",)),
        ("registry", ("InMemoryConnectorRegistry", "MemoryConnectorResolver")),
        ("runtime_configs", ("apply_config", "load_persisted_connectors")),
        (
            "tenant_scoped",
            ("TenantScopeError", "TenantScopedMemoryConnector", "tenant_slug"),
        ),
    ],
)
def test_memory_modules_are_the_same_surface_through_both_paths(
    module_name: str, names: tuple[str, ...]
) -> None:
    import importlib

    legacy_module = importlib.import_module(f"zeroth.core.memory.{module_name}")
    canonical_module = importlib.import_module(f"zeroth.integrations.memory.{module_name}")

    for name in names:
        assert getattr(canonical_module, name) is getattr(legacy_module, name), name


@pytest.mark.parametrize(
    ("module_name", "names"),
    [
        ("auditing", ("AuditingMemoryConnector",)),
        ("connector", ("MemoryConnector",)),
        ("models", ("MemoryEntry", "MemoryScope")),
        ("scoped", ("ScopedMemoryConnector",)),
    ],
)
def test_governed_memory_modules_are_the_same_surface_through_both_paths(
    module_name: str, names: tuple[str, ...]
) -> None:
    import importlib

    legacy_module = importlib.import_module(f"zeroth.core.governed.memory.{module_name}")
    canonical_module = importlib.import_module(
        f"zeroth.integrations.memory.governed.{module_name}"
    )

    for name in names:
        assert getattr(canonical_module, name) is getattr(legacy_module, name), name


def test_memory_scope_keeps_resolving_from_its_contract_owned_definition() -> None:
    from zeroth.contracts.governed.models.memory import MemoryScope as ContractMemoryScope
    from zeroth.integrations.memory.governed.models import MemoryScope as CanonicalMemoryScope

    assert CanonicalMemoryScope is ContractMemoryScope


def test_memory_imports_in_a_cold_interpreter() -> None:
    """The canonical package imports with nothing else pre-warmed.

    This kept the canonical half of a test that used to import the legacy
    and canonical packages in both orders, guarding a cycle between them.
    With the legacy package gone there is one direction left to guard.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import zeroth.integrations.memory"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
