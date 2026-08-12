from __future__ import annotations

import pytest

from zeroth.governance.retention.cleanup_state_repository import CleanupStateRepository
from zeroth.governance.retention.manifests import build_cleanup_manifest
from zeroth.governance.retention.models import ErasureResult
from zeroth.platform.storage import NullWorkspaceScopeContext
from tests.conftest import requires_docker


def _cleanup_manifest(tenant_id: str):
    return build_cleanup_manifest(
        ErasureResult(run_id="run-a", tenant_id=tenant_id, reason="rte"),
        [],
        ["run-a"],
        artifact_store=None,
        econ_eraser=None,
    )


async def _assert_rejected_without_partial_state(database, mismatch: str) -> None:
    authorization_log_id = f"authorization-{mismatch}"
    repository = CleanupStateRepository(
        database, NullWorkspaceScopeContext(tenant_id="tenant-a")
    )
    manifest = _cleanup_manifest("tenant-b" if mismatch == "manifest" else "tenant-a")
    if mismatch == "operation":
        manifest.operations[0] = manifest.operations[0].model_copy(
            update={"tenant_id": "tenant-b"}
        )
    async with database.transaction() as connection:
        await connection.execute(
            """
            INSERT INTO retention_audit_log (
                tenant_id, log_id, run_id, action, reason, detail, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "tenant-a",
                authorization_log_id,
                "run-a",
                "authorized",
                "rte",
                None,
                "2026-08-12T00:00:00+00:00",
            ),
        )
        with pytest.raises(ValueError, match="bound scope|manifest"):
            await repository.initialize_in_transaction(
                connection,
                authorization_log_id=authorization_log_id,
                manifest=manifest,
            )
        for table in ("retention_cleanup_state", "retention_cleanup_operations"):
            row = await connection.fetch_one(
                f"SELECT COUNT(*) AS row_count FROM {table} WHERE authorization_log_id = ?",
                (authorization_log_id,),
            )
            assert row["row_count"] == 0


@pytest.mark.parametrize("mismatch", ["manifest", "operation"])
async def test_cleanup_initialization_rejects_scope_mismatch_without_partial_state(
    async_database, mismatch: str
) -> None:
    await _assert_rejected_without_partial_state(async_database, mismatch)


@requires_docker
@pytest.mark.parametrize("mismatch", ["manifest", "operation"])
async def test_postgres_cleanup_scope_mismatch_is_atomic(postgres_database, mismatch: str) -> None:
    await _assert_rejected_without_partial_state(postgres_database, mismatch)
