"""Tenant-scoped artifact namespace regression tests."""

from __future__ import annotations

import asyncio
import base64
import fnmatch
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from zeroth.platform.artifacts.errors import ArtifactNotFoundError, ArtifactStorageError
from zeroth.platform.artifacts.models import generate_artifact_key
from zeroth.platform.artifacts.store import FilesystemArtifactStore, RedisArtifactStore
from zeroth.platform.artifacts.tenant_scoped import (
    TenantScopedArtifactStore,
    frame_artifact_key,
)


class _Pipeline:
    def __init__(self, redis: _MemoryRedis) -> None:
        self.redis = redis
        self.operations: list[tuple[str, tuple[Any, ...]]] = []

    async def __aenter__(self) -> _Pipeline:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def set(self, *args: Any) -> None:
        self.operations.append(("set", args))

    def setex(self, *args: Any) -> None:
        self.operations.append(("set", (args[0], args[2])))

    def delete(self, *args: Any) -> None:
        self.operations.append(("delete", args))

    def expire(self, *args: Any) -> None:
        self.operations.append(("expire", args))

    async def execute(self) -> list[object]:
        return [await getattr(self.redis, operation)(*args) for operation, args in self.operations]


class _MemoryRedis:
    """Small shared Redis fake faithful to the methods used by the backend."""

    def __init__(self, values: dict[str, bytes] | None = None) -> None:
        self.values = values if values is not None else {}
        self.scan_patterns: list[str] = []

    def pipeline(self, *, transaction: bool = True) -> _Pipeline:
        assert transaction is True
        return _Pipeline(self)

    async def get(self, key: str) -> bytes | None:
        return self.values.get(key)

    async def set(self, key: str, value: object, *, nx: bool = False) -> bool:
        if nx and key in self.values:
            return False
        self.values[key] = str(value).encode() if not isinstance(value, bytes) else value
        return True

    async def setex(self, key: str, _ttl: int, value: bytes) -> bool:
        self.values[key] = value
        return True

    async def delete(self, *keys: str | bytes) -> int:
        count = 0
        for raw_key in keys:
            key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
            count += int(self.values.pop(key, None) is not None)
        return count

    async def exists(self, key: str) -> int:
        return int(key in self.values)

    async def expire(self, key: str, _ttl: int) -> int:
        return int(key in self.values)

    async def scan_iter(self, *, match: str, count: int = 100):
        assert count == 100
        self.scan_patterns.append(match)
        for key in tuple(self.values):
            if fnmatch.fnmatchcase(key, match):
                yield key.encode()


class _ContendedMemoryRedis(_MemoryRedis):
    """Hold two NX receipt writes so competing operations observe no receipt."""

    def __init__(self) -> None:
        super().__init__()
        self._receipt_writers = 0
        self._release_receipts = asyncio.Event()

    async def set(self, key: str, value: object, *, nx: bool = False) -> bool:
        if nx and ":erasure-receipt:" in key:
            self._receipt_writers += 1
            if self._receipt_writers == 2:
                self._release_receipts.set()
            await self._release_receipts.wait()
        return await super().set(key, value, nx=nx)


def _filesystem_store(path: Path) -> FilesystemArtifactStore:
    return FilesystemArtifactStore(path, default_ttl=3600)


def _redis_store(values: dict[str, bytes]) -> tuple[RedisArtifactStore, _MemoryRedis]:
    client = _MemoryRedis(values)
    return RedisArtifactStore("", prefix="shared", client=client), client


@pytest.mark.asyncio()
@pytest.mark.parametrize("backend", ["filesystem", "redis"])
async def test_shared_backend_isolates_full_artifact_lifecycle(
    backend: str, tmp_path: Path
) -> None:
    shared_values: dict[str, bytes] = {}
    if backend == "filesystem":
        underlying_a = underlying_b = _filesystem_store(tmp_path)
    else:
        underlying_a, _ = _redis_store(shared_values)
        underlying_b, _ = _redis_store(shared_values)

    tenant_a = TenantScopedArtifactStore(underlying_a, tenant_id="tenant-a", workspace_id=None)
    tenant_b = TenantScopedArtifactStore(underlying_b, tenant_id="tenant-b", workspace_id=None)
    key = "run-1/node/artifact"
    ref_a = await tenant_a.store(key, b"A", "text/plain", ttl=60)
    ref_b = await tenant_b.store(key, b"B", "text/plain", ttl=60)

    assert ref_a.key == ref_b.key == key
    assert "scopes/v1" not in ref_a.key
    assert await tenant_a.retrieve(key) == b"A"
    assert await tenant_b.retrieve(key) == b"B"
    assert await tenant_a.exists(key) and await tenant_b.exists(key)
    assert await tenant_a.refresh_ttl(key, 120)
    assert await tenant_a.delete(key, idempotency_key="same-receipt") is True
    assert await tenant_a.delete(key, idempotency_key="same-receipt") is True
    assert await tenant_b.exists(key)
    assert await tenant_b.delete(key, idempotency_key="same-receipt") is True

    await tenant_a.store("cleanup-run/n1/a", b"a", "text/plain")
    await tenant_a.store("cleanup-run/n2/b", b"b", "text/plain")
    await tenant_b.store("cleanup-run/n1/a", b"foreign", "text/plain")
    expected_cleanup_count = 2 if backend == "filesystem" else 4
    assert (
        await tenant_a.cleanup_run("cleanup-run", idempotency_key="same-cleanup")
        == expected_cleanup_count
    )
    assert (
        await tenant_a.cleanup_run("cleanup-run", idempotency_key="same-cleanup")
        == expected_cleanup_count
    )
    assert await tenant_b.retrieve("cleanup-run/n1/a") == b"foreign"


@pytest.mark.asyncio()
async def test_restart_keeps_filesystem_scope_and_missing_errors_logical(tmp_path: Path) -> None:
    first = TenantScopedArtifactStore(
        _filesystem_store(tmp_path), tenant_id="tenant", workspace_id="workspace"
    )
    await first.store("run/node/key", b"persisted", "text/plain")

    restarted = TenantScopedArtifactStore(
        _filesystem_store(tmp_path), tenant_id="tenant", workspace_id="workspace"
    )
    foreign = TenantScopedArtifactStore(
        _filesystem_store(tmp_path), tenant_id="tenant", workspace_id=None
    )
    assert await restarted.retrieve("run/node/key") == b"persisted"
    with pytest.raises(ArtifactNotFoundError, match=r"run/node/key") as exc_info:
        await foreign.retrieve("run/node/key")
    assert "scopes/v1" not in str(exc_info.value)
    assert await foreign.delete("run/node/key", idempotency_key="foreign-delete") is False
    assert await restarted.retrieve("run/node/key") == b"persisted"


def test_scope_digest_uses_exact_canonical_json() -> None:
    store = TenantScopedArtifactStore(object(), tenant_id="tenant", workspace_id=None)
    canonical = json.dumps(
        {"tenant_id": "tenant", "workspace_id": None},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    expected = hashlib.sha256(b"zeroth-artifact-scope-v1\0" + canonical).hexdigest()
    assert store.scope_digest == expected


@pytest.mark.asyncio()
async def test_physical_segments_and_receipt_id_use_exact_encoding(tmp_path: Path) -> None:
    wrapper = TenantScopedArtifactStore(
        _filesystem_store(tmp_path), tenant_id="tenant", workspace_id="workspace"
    )
    logical_key = "run/n\u00f8de/.."
    logical_receipt = "delete/*?[]\\"

    await wrapper.store(logical_key, b"value", "text/plain")
    await wrapper.delete(logical_key, idempotency_key=logical_receipt)

    def encode(segment: str) -> str:
        raw = segment.encode("utf-8")
        return f"{len(raw)}-{base64.urlsafe_b64encode(raw).rstrip(b'=').decode()}"

    object_path = (
        tmp_path
        / "scopes"
        / "v1"
        / wrapper.scope_digest
        / "objects"
        / "v1"
        / encode("run")
        / encode("legacy")
        / encode("n\u00f8de")
        / encode("..")
    )
    receipt_digest = hashlib.sha256(logical_receipt.encode()).hexdigest()
    receipt_path = (
        tmp_path
        / ".erasure-receipts"
        / f"scope-v1-{wrapper.scope_digest}-receipt-{receipt_digest}.json"
    )
    assert not object_path.exists()
    assert receipt_path.exists()


@pytest.mark.asyncio()
async def test_adversarial_scopes_and_keys_have_distinct_opaque_paths(tmp_path: Path) -> None:
    scopes = [
        ("tenant/../*?[x]\\", None),
        ("tenant/../*?[x]\\", ""),
        ("tenant/../*?[x]\\", "null"),
        ("e\u0301", "workspace"),
        ("\u00e9", "workspace"),
    ]
    wrappers = [
        TenantScopedArtifactStore(_filesystem_store(tmp_path), tenant_id=t, workspace_id=w)
        for t, w in scopes
    ]
    key = "run*?[]\\/../n\u00f8de/e\u0301"
    for index, wrapper in enumerate(wrappers):
        await wrapper.store(key, str(index).encode(), "text/plain")

    assert [await wrapper.retrieve(key) for wrapper in wrappers] == [
        str(index).encode() for index in range(len(wrappers))
    ]
    object_dirs = list((tmp_path / "scopes" / "v1").glob("*/objects/v1"))
    assert len(object_dirs) == len(scopes)
    all_paths = "\n".join(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    for raw in ("tenant", "workspace", "run", "n\u00f8de", "e\u0301", "\u00e9"):
        assert raw not in all_paths


@pytest.mark.parametrize(
    ("tenant_id", "workspace_id"),
    [("bad\x00tenant", None), ("tenant", "bad\x00workspace")],
)
def test_scope_rejects_nul(tenant_id: str, workspace_id: str | None) -> None:
    with pytest.raises(ArtifactStorageError, match="NUL"):
        TenantScopedArtifactStore(object(), tenant_id=tenant_id, workspace_id=workspace_id)


@pytest.mark.asyncio()
async def test_logical_identifiers_reject_nul(tmp_path: Path) -> None:
    wrapper = TenantScopedArtifactStore(_filesystem_store(tmp_path), tenant_id="t")
    with pytest.raises(ArtifactStorageError, match="NUL"):
        await wrapper.store("run/bad\x00key", b"x", "text/plain")
    with pytest.raises(ArtifactStorageError, match="NUL"):
        await wrapper.cleanup_run("bad\x00run", idempotency_key="receipt")
    with pytest.raises(ArtifactStorageError, match="NUL"):
        await wrapper.delete("run/key", idempotency_key="bad\x00receipt")


@pytest.mark.asyncio()
async def test_redis_cleanup_pattern_contains_only_encoded_run() -> None:
    values: dict[str, bytes] = {}
    backend, client = _redis_store(values)
    wrapper = TenantScopedArtifactStore(backend, tenant_id="tenant/*?[]\\")
    run_id = "run*?[]\\"
    await wrapper.store(f"{run_id}/node/key", b"x", "text/plain")

    assert await wrapper.cleanup_run(run_id, idempotency_key="receipt*?[]\\") == 2
    pattern = client.scan_patterns[-1]
    assert run_id not in pattern
    assert "tenant" not in pattern
    assert pattern.endswith("/*")


@pytest.mark.asyncio()
@pytest.mark.parametrize("backend", ["filesystem", "redis"])
async def test_slash_bearing_run_cleanup_uses_explicit_framing(
    backend: str, tmp_path: Path
) -> None:
    values: dict[str, bytes] = {}
    underlying = _filesystem_store(tmp_path) if backend == "filesystem" else _redis_store(values)[0]
    wrapper = TenantScopedArtifactStore(underlying, tenant_id="tenant")
    nested_run_key = frame_artifact_key("a/b", f"node/{'a' * 32}")
    parent_run_key = frame_artifact_key("a", f"b/{'b' * 32}")
    await wrapper.store(nested_run_key, b"nested", "text/plain")
    await wrapper.store(parent_run_key, b"parent", "text/plain")

    deleted = await wrapper.cleanup_run("a/b", idempotency_key="nested-cleanup")

    assert deleted == (1 if backend == "filesystem" else 2)
    assert not await wrapper.exists(nested_run_key)
    assert await wrapper.retrieve(parent_run_key) == b"parent"
    assert nested_run_key != parent_run_key


@pytest.mark.asyncio()
async def test_production_generated_slash_run_key_cleans_up(tmp_path: Path) -> None:
    wrapper = TenantScopedArtifactStore(_filesystem_store(tmp_path), tenant_id="tenant")
    key = generate_artifact_key("slash/run", "node")
    reference = await wrapper.store(key, b"payload", "text/plain")

    assert await wrapper.cleanup_run("slash/run", idempotency_key="cleanup") == 1
    assert not await wrapper.exists(key)
    assert reference.key == key


@pytest.mark.asyncio()
async def test_historical_marker_key_survives_foreign_framed_owner_cleanup(
    tmp_path: Path,
) -> None:
    wrapper = TenantScopedArtifactStore(_filesystem_store(tmp_path), tenant_id="tenant")
    historical = f"zeroth-run-v1/1-YQ/{'a' * 32}"
    await wrapper.store(historical, b"historical", "text/plain")

    assert await wrapper.retrieve(historical) == b"historical"
    assert await wrapper.cleanup_run("a", idempotency_key="foreign-cleanup") == 0
    assert await wrapper.retrieve(historical) == b"historical"
    with pytest.raises(ArtifactStorageError, match="Malformed framed artifact key"):
        await wrapper.store(
            "zeroth-run-v1/1-YQ/not-a-generated-key",
            b"ambiguous",
            "text/plain",
        )


@pytest.mark.asyncio()
async def test_redis_cleanup_counts_data_key_ending_meta() -> None:
    values: dict[str, bytes] = {}
    backend, _ = _redis_store(values)
    await backend.store("run/data:meta", b"payload", "text/plain")

    assert await backend.cleanup_run("run", idempotency_key="cleanup") == 2


@pytest.mark.asyncio()
async def test_redis_receipts_bind_operation_and_target() -> None:
    values: dict[str, bytes] = {}
    backend, _ = _redis_store(values)
    await backend.store("run/a", b"a", "text/plain")
    await backend.store("run/b", b"b", "text/plain")

    assert await backend.delete("run/a", idempotency_key="receipt") is True
    assert await backend.delete("run/a", idempotency_key="receipt") is True
    with pytest.raises(ArtifactStorageError, match="reused for another operation"):
        await backend.delete("run/b", idempotency_key="receipt")
    with pytest.raises(ArtifactStorageError, match="reused for another operation"):
        await backend.cleanup_run("run", idempotency_key="receipt")
    assert await backend.retrieve("run/b") == b"b"


@pytest.mark.asyncio()
async def test_redis_cleanup_receipt_replay_and_target_misuse() -> None:
    values: dict[str, bytes] = {}
    backend, _ = _redis_store(values)
    await backend.store("run/a", b"a", "text/plain")

    assert await backend.cleanup_run("run", idempotency_key="cleanup") == 2
    assert await backend.cleanup_run("run", idempotency_key="cleanup") == 2
    with pytest.raises(ArtifactStorageError, match="reused for another operation"):
        await backend.cleanup_run("other", idempotency_key="cleanup")


@pytest.mark.asyncio()
@pytest.mark.parametrize("competitor", ["different-target", "different-operation"])
async def test_redis_competing_receipt_has_one_binding_winner(competitor: str) -> None:
    client = _ContendedMemoryRedis()
    backend = RedisArtifactStore("", prefix="shared", client=client)
    await backend.store("run/a", b"a", "text/plain")
    await backend.store("run/b", b"b", "text/plain")
    first = backend.delete("run/a", idempotency_key="contended")
    second = (
        backend.delete("run/b", idempotency_key="contended")
        if competitor == "different-target"
        else backend.cleanup_run("run", idempotency_key="contended")
    )

    results = await asyncio.gather(first, second, return_exceptions=True)

    winners = [index for index, result in enumerate(results) if not isinstance(result, Exception)]
    losers = [result for result in results if isinstance(result, Exception)]
    assert len(winners) == len(losers) == 1
    assert isinstance(losers[0], ArtifactStorageError)
    assert str(losers[0]) == "idempotency key reused for another operation"
    if winners[0] == 0:
        assert await backend.delete("run/a", idempotency_key="contended") is True
    elif competitor == "different-target":
        assert await backend.delete("run/b", idempotency_key="contended") is True
    else:
        assert await backend.cleanup_run("run", idempotency_key="contended") == results[1]
