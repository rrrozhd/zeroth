from __future__ import annotations

from zeroth.platform.dispatch.operations import SideEffectOperationStore
from zeroth.platform.storage import ScopeContext


async def test_side_effect_identity_and_erasure_are_scope_local(sqlite_db) -> None:
    owner = SideEffectOperationStore(
        sqlite_db,
        ScopeContext(tenant_id="tenant-a", workspace_id="workspace-a"),
    )
    foreign = SideEffectOperationStore(
        sqlite_db,
        ScopeContext(tenant_id="tenant-b", workspace_id="workspace-b"),
    )
    claim = {
        "run_id": "shared-run",
        "dispatch_id": "shared-dispatch",
        "idempotency_key": "shared-idempotency",
        "target_ref": "unit://shared",
        "attempt": 0,
    }

    owner_claim = await owner.claim("shared-operation", **claim)
    foreign_claim = await foreign.claim("shared-operation", **claim)
    assert owner_claim.first_execution is True
    assert foreign_claim.first_execution is True

    assert await owner.complete("shared-operation", receipt="owner") is True
    assert await foreign.complete("shared-operation", receipt="foreign") is True
    assert (await owner.get("shared-operation"))["receipt"] == "owner"
    assert (await foreign.get("shared-operation"))["receipt"] == "foreign"

    assert await foreign.erase_for_run("shared-run") == 1
    assert await foreign.get("shared-operation") is None
    assert (await owner.get("shared-operation"))["receipt"] == "owner"


async def test_side_effect_foreign_scope_matches_unknown(sqlite_db) -> None:
    owner = SideEffectOperationStore(
        sqlite_db,
        ScopeContext(tenant_id="tenant-a", workspace_id="workspace-a"),
    )
    other_workspace = SideEffectOperationStore(
        sqlite_db,
        ScopeContext(tenant_id="tenant-a", workspace_id="workspace-b"),
    )
    await owner.claim(
        "operation-a",
        run_id="run-a",
        dispatch_id="dispatch-a",
        idempotency_key="idempotency-a",
        target_ref="unit://a",
    )

    assert await other_workspace.get("operation-a") is None
    assert await other_workspace.complete("operation-a", receipt="forged") is False
    await other_workspace.fail("operation-a", error="forged")
    await other_workspace.mark_ambiguous("operation-a", reason="forged")
    assert (
        await other_workspace.record_reconciliation(
            "operation-a",
            resolved=True,
            receipt="forged",
        )
    ).value == "NOT_STARTED"
    assert await other_workspace.erase_for_run("run-a") == 0
    owner_record = await owner.get("operation-a")
    assert owner_record is not None
    assert owner_record["state"] == "IN_FLIGHT"
