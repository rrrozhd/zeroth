"""HTTP tests for the sidecar workspace-staging endpoint (ZER-37)."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from zeroth.integrations.sandbox.app import app
from zeroth.integrations.sandbox.staging import (
    TarSummary,
    WorkspaceStore,
    WorkspaceValidationCode,
    WorkspaceValidationError,
)

SIDECAR_SECRET = "test-sidecar-secret"
SIDECAR_SECRET_HEADER = "X-Zeroth-Sandbox-Secret"
CANARY = "CANARY-51ac"


def _tar_payload(files: dict[str, bytes] | None = None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT) as archive:
        for name, data in (files or {"main.py": b"print('hi')"}).items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _client(headers: dict[str, str] | None = None) -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test", headers=headers or {})


@pytest.fixture
def mock_store() -> AsyncMock:
    return AsyncMock(spec=WorkspaceStore)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configured_secret", "presented_secret"),
    [
        (None, SIDECAR_SECRET),
        (SIDECAR_SECRET, None),
        (SIDECAR_SECRET, "wrong-secret"),
    ],
)
async def test_workspace_upload_rejects_unauthorized_calls_before_effects(
    mock_store: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    configured_secret: str | None,
    presented_secret: str | None,
) -> None:
    """The staging route fails closed exactly like the pinned /execute matrix."""
    if configured_secret is None:
        monkeypatch.delenv("ZEROTH_SANDBOX_SIDECAR_SECRET", raising=False)
    else:
        monkeypatch.setenv("ZEROTH_SANDBOX_SIDECAR_SECRET", configured_secret)
    headers = {} if presented_secret is None else {SIDECAR_SECRET_HEADER: presented_secret}
    with patch("zeroth.integrations.sandbox.app.workspace_store", mock_store):
        async with _client(headers) as unauthenticated:
            response = await unauthenticated.put(
                "/workspaces/forbidden", content=_tar_payload()
            )

    assert response.status_code == 401
    mock_store.ingest.assert_not_awaited()


@pytest.mark.asyncio
async def test_workspace_upload_returns_201_with_the_staging_summary(
    mock_store: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZEROTH_SANDBOX_SIDECAR_SECRET", SIDECAR_SECRET)
    mock_store.ingest.return_value = TarSummary(
        member_count=2, total_file_bytes=10, raw_bytes=2048
    )
    with patch("zeroth.integrations.sandbox.app.workspace_store", mock_store):
        async with _client({SIDECAR_SECRET_HEADER: SIDECAR_SECRET}) as client:
            response = await client.put("/workspaces/ws-201", content=_tar_payload())

    assert response.status_code == 201
    assert response.json() == {
        "workspace_id": "ws-201",
        "raw_bytes": 2048,
        "member_count": 2,
        "total_file_bytes": 10,
    }
    mock_store.ingest.assert_awaited_once()
    assert mock_store.ingest.await_args.args[0] == "ws-201"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "expected_status"),
    [
        (WorkspaceValidationCode.WORKSPACE_DUPLICATE, 409),
        (WorkspaceValidationCode.TAR_TOO_LARGE, 413),
        (WorkspaceValidationCode.TAR_MEMBER_FORBIDDEN, 422),
        (WorkspaceValidationCode.TAR_MEMBER_TRAVERSAL, 422),
        (WorkspaceValidationCode.TAR_COMPRESSED, 422),
        (WorkspaceValidationCode.WORKSPACE_ID_INVALID, 422),
    ],
)
async def test_workspace_upload_maps_refusals_to_statuses_with_generic_details(
    mock_store: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    code: WorkspaceValidationCode,
    expected_status: int,
) -> None:
    monkeypatch.setenv("ZEROTH_SANDBOX_SIDECAR_SECRET", SIDECAR_SECRET)
    mock_store.ingest.side_effect = WorkspaceValidationError(code)
    with patch("zeroth.integrations.sandbox.app.workspace_store", mock_store):
        async with _client({SIDECAR_SECRET_HEADER: SIDECAR_SECRET}) as client:
            response = await client.put("/workspaces/ws-err", content=_tar_payload())

    assert response.status_code == expected_status
    detail = response.json()["detail"]
    assert detail == str(WorkspaceValidationError(code))  # the fixed template
    assert CANARY not in detail


@pytest.mark.asyncio
async def test_workspace_upload_round_trips_through_a_real_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End to end through the route: stream in, spool, validate, register."""
    monkeypatch.setenv("ZEROTH_SANDBOX_SIDECAR_SECRET", SIDECAR_SECRET)
    store = WorkspaceStore(tmp_path)
    payload = _tar_payload({"pkg/main.py": b"print('hi')", "pkg/util.py": b"pass"})
    with patch("zeroth.integrations.sandbox.app.workspace_store", store):
        async with _client({SIDECAR_SECRET_HEADER: SIDECAR_SECRET}) as client:
            accepted = await client.put("/workspaces/ws-real", content=payload)
            duplicate = await client.put("/workspaces/ws-real", content=payload)
            hostile = await client.put(
                "/workspaces/ws-hostile",
                content=_hostile_symlink_tar(),
            )

    assert accepted.status_code == 201
    body = accepted.json()
    assert body["member_count"] == 2
    assert body["raw_bytes"] == len(payload)
    assert duplicate.status_code == 409
    assert hostile.status_code == 422
    assert CANARY not in hostile.json()["detail"]
    assert (await store.claim("ws-real")).read_bytes() == payload


def _hostile_symlink_tar() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT) as archive:
        info = tarfile.TarInfo(f"{CANARY}-link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        archive.addfile(info)
    return buffer.getvalue()
