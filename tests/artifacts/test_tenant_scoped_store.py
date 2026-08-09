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
        if any(operation == "delete" for operation, _args in self.operations):
            self.redis.fail_erasure_if_requested()
        return [await getattr(self.redis, operation)(*args) for operation, args in self.operations]


class _MemoryRedis:
    """Small shared Redis fake faithful to the methods used by the backend."""

    def __init__(self, values: dict[str, bytes] | None = None) -> None:
        self.values = values if values is not None else {}
        self.scan_patterns: list[str] = []
        self.delete_calls: list[tuple[str | bytes, ...]] = []
        self.fail_next_erasure = False
        self.fail_erasure_stage: str | None = None
        self.eval_calls: list[tuple[int, tuple[object, ...]]] = []
        self.erasure_mutations = 0

    def fail_erasure_if_requested(self) -> None:
        if self.fail_next_erasure:
            self.fail_next_erasure = False
            raise RuntimeError("injected erasure failure")

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
        self.fail_erasure_if_requested()
        self.delete_calls.append(keys)
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

    async def eval(self, script: str, numkeys: int, *items: object) -> list[bytes]:
        """Model the atomic receipt/mutation Lua state machine used in production."""
        assert script == RedisArtifactStore._ERASURE_SCRIPT
        self.eval_calls.append((numkeys, items))
        self.fail_erasure_if_requested()

        def _text(value: object) -> str:
            return value.decode() if isinstance(value, bytes) else str(value)

        keys = [_text(value) for value in items[:numkeys]]
        arguments = [_text(value) for value in items[numkeys:]]
        kind, target = arguments[:2]

        def _validated(raw: bytes, *, prior: bool) -> tuple[str, bool | int | None]:
            try:
                receipt = json.loads(raw)
            except (TypeError, ValueError):
                return ("legacy" if prior else "invalid"), None
            if not isinstance(receipt, dict):
                return ("legacy" if prior else "invalid"), None
            version = receipt.get("version")
            if (not prior and version != 2) or (prior and version not in (None, 2)):
                return "mismatch", None
            if receipt.get("kind") != kind or receipt.get("target") != target:
                return "mismatch", None
            result = receipt.get("result")
            if kind == "delete" and type(result) is not bool:
                return "invalid", None
            if kind == "cleanup_run" and (type(result) is not int or result < 0):
                return "invalid", None
            return "ok", result

        completed = self.values.get(keys[0])
        if completed is not None:
            status, result = _validated(completed, prior=False)
            return [status.encode()] if status != "ok" else [b"ok", json.dumps(result).encode()]

        prior_receipt = self.values.get(keys[1])
        if prior_receipt is not None:
            status, result = _validated(prior_receipt, prior=True)
            if status != "ok":
                return [status.encode()]
            self.values[keys[0]] = json.dumps(
                {"version": 2, "kind": kind, "target": target, "result": result}
            ).encode()
            return [b"ok", json.dumps(result).encode()]

        if kind == "delete":
            legacy_enabled = arguments[2] == "1"
            result = keys[2] in self.values or (legacy_enabled and keys[4] in self.values)
            delete_keys = keys[2:4] + (keys[4:6] if legacy_enabled else [])
        else:
            new_exact, meta_namespace, legacy_exact, new_namespace, legacy_namespace = arguments[
                2:7
            ]
            legacy_enabled = arguments[7] == "1"
            new_keys = [key for key in tuple(self.values) if key.startswith(new_exact)]
            legacy_keys = (
                [key for key in tuple(self.values) if key.startswith(legacy_exact)]
                if legacy_enabled
                else []
            )
            logical = {key.removeprefix(new_namespace) for key in new_keys}
            logical.update(
                key.removeprefix(legacy_namespace)
                for key in legacy_keys
                if f"{key}:meta" in self.values
            )
            result = len(logical)
            delete_keys = []
            for key in new_keys:
                delete_keys.extend([key, f"{meta_namespace}{key.removeprefix(new_namespace)}"])
            for key in legacy_keys:
                delete_keys.append(key)

        if len(set(delete_keys)) > 1000:
            return [b"too_many"]
        if self.fail_erasure_stage == "delete_acl":
            self.fail_erasure_stage = None
            return [b"forbidden"]
        if self.fail_erasure_stage == "receipt_write":
            self.fail_erasure_stage = None
            raise RuntimeError("injected receipt write failure")

        self.values[keys[0]] = json.dumps(
            {"version": 2, "kind": kind, "target": target, "result": result}
        ).encode()
        for key in set(delete_keys):
            self.values.pop(key, None)
        self.erasure_mutations += 1
        return [b"ok", json.dumps(result).encode()]


class _ContendedMemoryRedis(_MemoryRedis):
    """Release two erasure scripts together, then execute each atomically."""

    def __init__(self) -> None:
        super().__init__()
        self._eval_waiters = 0
        self._release_evals = asyncio.Event()

    async def eval(self, script: str, numkeys: int, *items: object) -> list[bytes]:
        self._eval_waiters += 1
        if self._eval_waiters == 2:
            self._release_evals.set()
        await self._release_evals.wait()
        return await super().eval(script, numkeys, *items)


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
    expected_cleanup_count = 2
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

    assert await wrapper.cleanup_run(run_id, idempotency_key="receipt*?[]\\") == 1
    numkeys, items = client.eval_calls[-1]
    pattern = str(items[numkeys + 8])
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

    assert deleted == 1
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

    assert await backend.cleanup_run("run", idempotency_key="cleanup") == 1


@pytest.mark.asyncio()
@pytest.mark.parametrize("reserved_run", ["data:foo", "meta:foo"])
async def test_redis_reserved_legacy_run_never_cross_deletes_v2_namespace(
    reserved_run: str,
) -> None:
    values: dict[str, bytes] = {}
    backend, _ = _redis_store(values)
    await backend.store("foo/node", b"unrelated", "text/plain")
    await backend.store(f"{reserved_run}/node", b"reserved", "text/plain")

    assert await backend.cleanup_run(reserved_run, idempotency_key="cleanup") == 1

    assert await backend.retrieve("foo/node") == b"unrelated"
    assert await backend.exists("foo/node")
    assert not await backend.exists(f"{reserved_run}/node")
    assert "shared:meta:foo/node" in values


@pytest.mark.asyncio()
async def test_redis_reserved_key_delete_never_cross_deletes_v2_namespace() -> None:
    values: dict[str, bytes] = {}
    backend, _ = _redis_store(values)
    await backend.store("foo/node", b"unrelated", "text/plain")
    await backend.store("data:foo/node", b"reserved", "text/plain")

    assert await backend.delete("data:foo/node", idempotency_key="delete") is True

    assert await backend.retrieve("foo/node") == b"unrelated"
    assert not await backend.exists("data:foo/node")
    assert "shared:meta:foo/node" in values


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

    assert await backend.cleanup_run("run", idempotency_key="cleanup") == 1
    assert await backend.cleanup_run("run", idempotency_key="cleanup") == 1
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
    assert client.erasure_mutations == 1
    assert isinstance(losers[0], ArtifactStorageError)
    assert str(losers[0]) == "idempotency key reused for another operation"
    if winners[0] == 0:
        assert await backend.delete("run/a", idempotency_key="contended") is True
    elif competitor == "different-target":
        assert await backend.delete("run/b", idempotency_key="contended") is True
    else:
        assert await backend.cleanup_run("run", idempotency_key="contended") == results[1]


@pytest.mark.asyncio()
async def test_redis_legacy_receipt_blocks_unbound_reexecution() -> None:
    values: dict[str, bytes] = {
        "shared:erasure-receipt:legacy": b"1",
        "shared:data:run/a": b"a",
        "shared:meta:run/a": b"{}",
    }
    backend, _ = _redis_store(values)

    with pytest.raises(ArtifactStorageError, match="Legacy erasure receipt"):
        await backend.delete("run/a", idempotency_key="legacy")
    assert await backend.retrieve("run/a") == b"a"


@pytest.mark.asyncio()
async def test_redis_legacy_layout_key_ending_meta_counts_once_after_restart() -> None:
    values: dict[str, bytes] = {
        "shared:run/data:meta": b"legacy-payload",
        "shared:run/data:meta:meta": b"{}",
    }
    backend, _ = _redis_store(values)
    assert await backend.retrieve("run/data:meta") == b"legacy-payload"

    restarted, _ = _redis_store(values)
    assert await restarted.cleanup_run("run", idempotency_key="cleanup") == 1
    assert not await restarted.exists("run/data:meta")


@pytest.mark.asyncio()
async def test_redis_delete_replay_does_not_delete_replacement() -> None:
    values: dict[str, bytes] = {}
    backend, client = _redis_store(values)
    await backend.store("run/a", b"first", "text/plain")
    assert await backend.delete("run/a", idempotency_key="delete") is True
    first_delete_count = len(client.delete_calls)
    await backend.store("run/a", b"replacement", "text/plain")

    assert await backend.delete("run/a", idempotency_key="delete") is True
    assert len(client.delete_calls) == first_delete_count
    assert client.erasure_mutations == 1
    assert await backend.retrieve("run/a") == b"replacement"


@pytest.mark.asyncio()
async def test_redis_failed_delete_has_no_receipt_and_retry_completes() -> None:
    values: dict[str, bytes] = {}
    backend, client = _redis_store(values)
    await backend.store("run/a", b"first", "text/plain")
    client.fail_next_erasure = True

    with pytest.raises(ArtifactStorageError, match="Redis erasure operation failed"):
        await backend.delete("run/a", idempotency_key="delete")

    assert "shared:erasure-receipt:v2:delete" not in values
    assert await backend.retrieve("run/a") == b"first"
    assert await backend.delete("run/a", idempotency_key="delete") is True
    await backend.store("run/a", b"replacement", "text/plain")
    assert await backend.delete("run/a", idempotency_key="delete") is True
    assert client.erasure_mutations == 1
    assert len(client.eval_calls) == 3
    assert await backend.retrieve("run/a") == b"replacement"


@pytest.mark.asyncio()
async def test_redis_cleanup_replay_does_not_scan_or_delete_replacement() -> None:
    values: dict[str, bytes] = {}
    backend, client = _redis_store(values)
    await backend.store("run/a", b"first", "text/plain")
    assert await backend.cleanup_run("run", idempotency_key="cleanup") == 1
    first_scan_count = len(client.scan_patterns)
    first_delete_count = len(client.delete_calls)
    await backend.store("run/b", b"replacement", "text/plain")

    assert await backend.cleanup_run("run", idempotency_key="cleanup") == 1
    assert len(client.scan_patterns) == first_scan_count
    assert len(client.delete_calls) == first_delete_count
    assert client.erasure_mutations == 1
    assert await backend.retrieve("run/b") == b"replacement"


@pytest.mark.asyncio()
async def test_redis_failed_cleanup_has_no_receipt_and_retry_completes() -> None:
    values: dict[str, bytes] = {}
    backend, client = _redis_store(values)
    await backend.store("run/a", b"first", "text/plain")
    client.fail_next_erasure = True

    with pytest.raises(ArtifactStorageError, match="Redis erasure operation failed"):
        await backend.cleanup_run("run", idempotency_key="cleanup")

    assert "shared:erasure-receipt:v2:cleanup" not in values
    assert await backend.retrieve("run/a") == b"first"
    assert await backend.cleanup_run("run", idempotency_key="cleanup") == 1
    await backend.store("run/b", b"replacement", "text/plain")
    assert await backend.cleanup_run("run", idempotency_key="cleanup") == 1
    assert client.erasure_mutations == 1
    assert len(client.eval_calls) == 3
    assert await backend.retrieve("run/b") == b"replacement"


@pytest.mark.asyncio()
@pytest.mark.parametrize("operation", ["delete", "cleanup_run"])
@pytest.mark.parametrize("failure_stage", ["receipt_write", "delete_acl"])
async def test_redis_erasure_command_failure_has_no_receipt_or_mutation(
    operation: str,
    failure_stage: str,
) -> None:
    values: dict[str, bytes] = {}
    backend, client = _redis_store(values)
    await backend.store("run/a", b"first", "text/plain")
    client.fail_erasure_stage = failure_stage

    with pytest.raises(ArtifactStorageError, match="Redis erasure operation failed"):
        if operation == "delete":
            await backend.delete("run/a", idempotency_key="failure")
        else:
            await backend.cleanup_run("run", idempotency_key="failure")

    assert "shared:erasure-receipt:v2:failure" not in values
    assert await backend.retrieve("run/a") == b"first"
    assert client.erasure_mutations == 0


@pytest.mark.asyncio()
@pytest.mark.parametrize(
    ("receipt_key", "receipt_value"),
    [
        ("shared:erasure-receipt:v2:malformed", b"not-json"),
        ("shared:erasure-receipt:malformed", b"not-json"),
        ("shared:erasure-receipt:v2:wrong-type", [b"list-member"]),
        ("shared:erasure-receipt:wrong-type", [b"list-member"]),
    ],
)
async def test_redis_invalid_receipt_never_mutates(
    receipt_key: str,
    receipt_value: object,
) -> None:
    values: dict[str, bytes] = {receipt_key: receipt_value}  # type: ignore[dict-item]
    backend, client = _redis_store(values)
    await backend.store("run/a", b"first", "text/plain")
    idempotency_key = "wrong-type" if receipt_key.endswith("wrong-type") else "malformed"

    with pytest.raises(ArtifactStorageError):
        await backend.delete("run/a", idempotency_key=idempotency_key)

    assert await backend.retrieve("run/a") == b"first"
    assert client.erasure_mutations == 0


@pytest.mark.asyncio()
async def test_redis_cleanup_above_atomic_key_bound_fails_without_mutation() -> None:
    values: dict[str, bytes] = {}
    for index in range(501):
        values[f"shared:data:run/{index}"] = b"data"
        values[f"shared:meta:run/{index}"] = b"{}"
    backend, client = _redis_store(values)

    with pytest.raises(ArtifactStorageError, match="Redis erasure operation failed"):
        await backend.cleanup_run("run", idempotency_key="bounded")

    assert "shared:erasure-receipt:v2:bounded" not in values
    assert len([key for key in values if key.startswith("shared:data:run/")]) == 501
    assert client.erasure_mutations == 0


@pytest.mark.asyncio()
@pytest.mark.parametrize("kind", ["delete", "cleanup_run"])
async def test_redis_prior_structured_receipt_replays_without_mutation(kind: str) -> None:
    target = "run/a" if kind == "delete" else "run"
    result: bool | int = True if kind == "delete" else 1
    values: dict[str, bytes] = {
        "shared:erasure-receipt:prior": json.dumps(
            {"kind": kind, "target": target, "result": result}
        ).encode(),
        "shared:data:run/a": b"replacement",
        "shared:meta:run/a": b"{}",
    }
    backend, client = _redis_store(values)

    replayed = (
        await backend.delete(target, idempotency_key="prior")
        if kind == "delete"
        else await backend.cleanup_run(target, idempotency_key="prior")
    )

    assert replayed == result
    assert await backend.retrieve("run/a") == b"replacement"
    assert client.scan_patterns == []
    assert client.delete_calls == []
    assert "shared:erasure-receipt:v2:prior" in values


@pytest.mark.asyncio()
async def test_redis_prior_structured_receipt_rejects_target_mismatch() -> None:
    values: dict[str, bytes] = {
        "shared:erasure-receipt:prior": json.dumps(
            {"kind": "delete", "target": "run/a", "result": True}
        ).encode(),
        "shared:data:run/b": b"replacement",
        "shared:meta:run/b": b"{}",
    }
    backend, _ = _redis_store(values)

    with pytest.raises(ArtifactStorageError, match="reused for another operation"):
        await backend.delete("run/b", idempotency_key="prior")
    assert await backend.retrieve("run/b") == b"replacement"
