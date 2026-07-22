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

import pytest


def test_storage_is_the_same_surface_through_both_paths() -> None:
    from zeroth.core import storage as legacy
    from zeroth.platform import storage as canonical

    assert canonical.AsyncConnection is legacy.AsyncConnection
    assert canonical.AsyncDatabase is legacy.AsyncDatabase
    assert canonical.AsyncSQLiteDatabase is legacy.AsyncSQLiteDatabase
    assert canonical.EncryptedField is legacy.EncryptedField
    assert canonical.Migration is legacy.Migration
    assert canonical.RedisConfig is legacy.RedisConfig
    assert canonical.RedisDeploymentMode is legacy.RedisDeploymentMode
    assert canonical.SQLiteDatabase is legacy.SQLiteDatabase
    assert canonical.create_database is legacy.create_database
    assert canonical.docker_container_running is legacy.docker_container_running
    assert canonical.ensure_and_lock_row is legacy.ensure_and_lock_row


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


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("zeroth.platform.storage", "zeroth.core.storage"),
        ("zeroth.core.storage", "zeroth.platform.storage"),
    ],
)
def test_storage_cold_imports_from_both_directions(first: str, second: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}\nimport {second}\n"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"cold import {first} then {second} failed:\n{result.stderr}"
