"""Lifecycle tests for the sidecar WorkspaceStore (ZER-37)."""

from __future__ import annotations

import asyncio
import io
import tarfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from zeroth.integrations.sandbox.staging import (
    WorkspaceStore,
    WorkspaceValidationCode,
    WorkspaceValidationError,
)


def _tar_payload(files: dict[str, bytes] | None = None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT) as archive:
        for name, data in (files or {"hello.txt": b"hi"}).items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


async def _stream(data: bytes, chunk_size: int = 1024) -> AsyncIterator[bytes]:
    for start in range(0, len(data), chunk_size):
        yield data[start : start + chunk_size]


async def _ingest(store: WorkspaceStore, workspace_id: str, data: bytes, **caps):
    caps.setdefault("max_raw_bytes", 1_048_576)
    return await store.ingest(workspace_id, _stream(data), **caps)


async def test_ingest_then_claim_returns_the_spooled_tar(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    payload = _tar_payload({"a.txt": b"payload"})

    summary = await _ingest(store, "ws-1", payload)
    spool = await store.claim("ws-1")

    assert summary.member_count == 1
    assert summary.raw_bytes == len(payload)
    assert spool.read_bytes() == payload
    assert spool.parent == tmp_path
    # The spool filename is sidecar-generated, never derived from client strings.
    assert "ws-1" not in spool.name


async def test_mid_stream_cap_rejects_and_leaves_the_id_reusable(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    payload = _tar_payload({"big.bin": b"x" * 4096})

    with pytest.raises(WorkspaceValidationError) as excinfo:
        await _ingest(store, "ws-cap", payload, max_raw_bytes=1000)

    assert excinfo.value.code is WorkspaceValidationCode.TAR_TOO_LARGE
    assert list(tmp_path.glob("*.tar")) == []  # partial spool deleted
    # The id was never registered, so a corrected retry succeeds.
    await _ingest(store, "ws-cap", _tar_payload())


async def test_invalid_tar_leaves_the_id_unregistered_for_retry(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)

    with pytest.raises(WorkspaceValidationError):
        await _ingest(store, "ws-retry", b"garbage-not-a-tar" * 64)

    assert list(tmp_path.glob("*.tar")) == []
    summary = await _ingest(store, "ws-retry", _tar_payload())
    assert summary.member_count == 1


async def test_registered_duplicate_id_is_refused(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    await _ingest(store, "ws-dup", _tar_payload())

    with pytest.raises(WorkspaceValidationError) as excinfo:
        await _ingest(store, "ws-dup", _tar_payload())

    assert excinfo.value.code is WorkspaceValidationCode.WORKSPACE_DUPLICATE


async def test_claim_twice_reports_consumed(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    await _ingest(store, "ws-once", _tar_payload())
    await store.claim("ws-once")

    with pytest.raises(WorkspaceValidationError) as excinfo:
        await store.claim("ws-once")

    assert excinfo.value.code is WorkspaceValidationCode.WORKSPACE_CONSUMED


async def test_claim_unknown_id_reports_unknown(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)

    with pytest.raises(WorkspaceValidationError) as excinfo:
        await store.claim("never-uploaded")

    assert excinfo.value.code is WorkspaceValidationCode.WORKSPACE_UNKNOWN


async def test_consumed_id_still_refuses_reupload(tmp_path: Path) -> None:
    """The seen-id set is permanent: consuming a workspace does not reopen its id."""
    store = WorkspaceStore(tmp_path)
    await _ingest(store, "ws-replay", _tar_payload())
    await store.claim("ws-replay")

    with pytest.raises(WorkspaceValidationError) as excinfo:
        await _ingest(store, "ws-replay", _tar_payload())

    assert excinfo.value.code is WorkspaceValidationCode.WORKSPACE_DUPLICATE


async def test_ttl_sweep_deletes_unconsumed_spools_but_keeps_ids(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    await _ingest(store, "ws-stale", _tar_payload())
    await asyncio.sleep(0.01)

    removed = await store.sweep(0.0)

    assert removed == 1
    assert list(tmp_path.glob("*.tar")) == []
    with pytest.raises(WorkspaceValidationError) as claim_error:
        await store.claim("ws-stale")
    assert claim_error.value.code is WorkspaceValidationCode.WORKSPACE_CONSUMED
    with pytest.raises(WorkspaceValidationError) as upload_error:
        await _ingest(store, "ws-stale", _tar_payload())
    assert upload_error.value.code is WorkspaceValidationCode.WORKSPACE_DUPLICATE


async def test_ttl_sweep_spares_fresh_and_consumed_spools(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    await _ingest(store, "ws-fresh", _tar_payload())
    await _ingest(store, "ws-used", _tar_payload())
    claimed = await store.claim("ws-used")

    removed = await store.sweep(3600.0)

    assert removed == 0
    assert claimed.exists()
    assert (await store.claim("ws-fresh")).exists()


async def test_startup_gc_removes_stale_spool_files(tmp_path: Path) -> None:
    (tmp_path / "deadbeef.tar").write_bytes(b"left over from a previous process")
    store = WorkspaceStore(tmp_path)

    removed = store.startup_gc()

    assert removed == 1
    assert list(tmp_path.glob("*.tar")) == []


async def test_startup_gc_spares_tracked_spools(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)
    await _ingest(store, "ws-live", _tar_payload())

    removed = store.startup_gc()

    assert removed == 0
    assert (await store.claim("ws-live")).exists()


async def test_invalid_workspace_id_is_rejected_before_any_spool(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path)

    with pytest.raises(WorkspaceValidationError) as excinfo:
        await _ingest(store, "bad/id", _tar_payload())

    assert excinfo.value.code is WorkspaceValidationCode.WORKSPACE_ID_INVALID
    assert list(tmp_path.glob("*.tar")) == []
