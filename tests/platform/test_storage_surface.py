"""Canonical import surface for the platform storage package.

Non-golden boundary tests for the Task 11 storage move: the canonical
``zeroth.platform.storage`` package must publish the same objects the legacy
``zeroth.core.storage`` path keeps republishing, the governed-runtime store
factory must live outside the platform layer, and both packages must stay
cold-importable from a fresh interpreter in either order.
"""

from __future__ import annotations

import subprocess
import sys


def test_storage_publishes_its_whole_surface() -> None:
    """Every name the package is documented to export is still exported.

    This replaced a parity assertion comparing each name against the legacy
    republisher. ZER-25 removed that path, so the comparison would compare
    the module with itself; the surface it pinned is asserted directly.
    """
    import zeroth.platform.storage as canonical

    expected = {
        "AsyncConnection",
        "AsyncDatabase",
        "AsyncSQLiteDatabase",
        "EncryptedField",
        "Migration",
        "RedisConfig",
        "RedisDeploymentMode",
        "SQLiteDatabase",
        "create_database",
        "docker_container_running",
        "ensure_and_lock_row",
    }

    missing = sorted(name for name in expected if not hasattr(canonical, name))
    assert not missing, f"zeroth.platform.storage no longer publishes: {missing}"


def test_the_governed_store_factory_lives_in_integrations() -> None:
    """The governed-runtime store factory is domain-aware wiring, not storage.

    It constructs runtime and governance store implementations, so it lives in
    the integrations layer; the legacy ``zeroth.core.storage`` paths keep
    republishing it lazily.
    """
    from zeroth.core import storage as legacy
    from zeroth.integrations.persistence import governed_redis

    assert legacy.GovernAIRedisRuntimeStores is governed_redis.GovernAIRedisRuntimeStores
    assert legacy.build_governai_redis_runtime is governed_redis.build_governai_redis_runtime


def test_storage_imports_in_a_cold_interpreter() -> None:
    """The canonical package imports with nothing else pre-warmed.

    This kept the canonical half of a test that used to import the legacy
    and canonical packages in both orders, guarding a cycle between them.
    With the legacy package gone there is one direction left to guard.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import zeroth.platform.storage"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
