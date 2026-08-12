from __future__ import annotations

import inspect

import pytest

from zeroth.governance.retention import (
    LegalHoldRepository,
    RetentionAuditLogRepository,
    RetentionPolicyRepository,
)
from zeroth.governance.retention.cleanup_state_repository import CleanupStateRepository
from zeroth.governance.retention.manifests import build_cleanup_manifest
from zeroth.governance.retention.models import ErasureResult
from zeroth.platform.storage import NullWorkspaceScopeContext


@pytest.mark.parametrize(
    "repository_type",
    [LegalHoldRepository, RetentionAuditLogRepository],
)
def test_retention_repository_constructors_require_scope_context(repository_type: type) -> None:
    parameters = inspect.signature(repository_type).parameters

    assert "scope_context" in parameters
    assert parameters["scope_context"].default is inspect.Parameter.empty


def test_policy_repository_preserves_default_compatibility_constructor() -> None:
    parameters = inspect.signature(RetentionPolicyRepository).parameters

    assert "scope_context" not in parameters
    assert list(parameters) == ["database", "default_policy"]

    scoped_parameters = inspect.signature(RetentionPolicyRepository.scoped).parameters
    assert scoped_parameters["scope_context"].default is inspect.Parameter.empty


async def test_foreign_legal_hold_read_and_list_match_unknown_scope(async_database) -> None:
    owner = LegalHoldRepository(async_database, NullWorkspaceScopeContext(tenant_id="tenant-a"))
    foreign = LegalHoldRepository(async_database, NullWorkspaceScopeContext(tenant_id="tenant-b"))
    placed = await owner.place(run_id="run-a", reason="review")

    assert await foreign.get(placed.hold_id) is None
    assert await foreign.get("unknown-hold") is None
    assert await foreign.list_for_tenant() == []


def _cleanup_manifest(tenant_id: str):
    return build_cleanup_manifest(
        ErasureResult(run_id="run-a", tenant_id=tenant_id, reason="rte"),
        [],
        ["run-a"],
        artifact_store=None,
        econ_eraser=None,
    )


@pytest.mark.parametrize("mismatch", ["manifest", "operation"])
async def test_cleanup_initialization_rejects_scope_mismatch_without_partial_state(
    async_database, mismatch: str
) -> None:
    repository = CleanupStateRepository(
        async_database, NullWorkspaceScopeContext(tenant_id="tenant-a")
    )
    manifest = _cleanup_manifest("tenant-b" if mismatch == "manifest" else "tenant-a")
    if mismatch == "operation":
        manifest.operations[0] = manifest.operations[0].model_copy(update={"tenant_id": "tenant-b"})
    async with async_database.transaction() as connection:
        await connection.execute(
            """
            INSERT INTO retention_audit_log (
                tenant_id, log_id, run_id, action, reason, detail, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "tenant-a",
                "authorization-a",
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
                authorization_log_id="authorization-a",
                manifest=manifest,
            )
        state = await connection.fetch_one(
            "SELECT COUNT(*) AS row_count FROM retention_cleanup_state "
            "WHERE authorization_log_id = ?",
            ("authorization-a",),
        )
        operations = await connection.fetch_one(
            "SELECT COUNT(*) AS row_count FROM retention_cleanup_operations "
            "WHERE authorization_log_id = ?",
            ("authorization-a",),
        )
        assert state["row_count"] == 0
        assert operations["row_count"] == 0
