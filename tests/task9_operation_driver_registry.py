"""Executable isolation drivers keyed by Task 9 registry operation.

Each case calls a production repository. Generated-ID append-only resources
prove isolation by showing that a foreign append does not alter the owner's
enumeration; collision-capable resources use the same logical identifier.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from functools import partial

import pytest

from zeroth.contracts.graph import TokenEngineSnapshot, TokenEngineSnapshotState
from zeroth.governance.retention.audit_log_repository import RetentionAuditLogRepository
from zeroth.governance.retention.cleanup_manifest import (
    CleanupManifest,
    CleanupOperation,
    DatabaseErasureOutcome,
    operation_id,
)
from zeroth.governance.retention.cleanup_state_repository import CleanupStateRepository
from zeroth.governance.retention.coordination import RetentionCoordinator
from zeroth.governance.retention.legal_hold_repository import LegalHoldRepository
from zeroth.governance.retention.models import RetentionPolicy
from zeroth.governance.retention.policy_repository import RetentionPolicyRepository
from zeroth.integrations.langgraph import InventoryCoverage, ToolDecisionKind
from zeroth.integrations.persistence.runs.checkpoint_store import CheckpointRowStore
from zeroth.integrations.persistence.runs.run_repository import RunRepository
from zeroth.integrations.persistence.runs.thread_repository import ThreadRepository
from zeroth.platform.dispatch.operations import OperationState, SideEffectOperationStore
from zeroth.platform.storage import AsyncDatabase, NullWorkspaceScopeContext, ResourceOperation
from zeroth.platform.storage.json import to_json_value
from zeroth.runtime.orchestration.token_snapshot_store import TokenSnapshotConcurrencyError
from zeroth.runtime.runs import Run, RunStatus, Thread
from zeroth.service.langgraph_gateway.enforcement import (
    DecisionResponseV1,
    InventoryEntryV1,
    InventoryRegistrationV1,
    inventory_fingerprint,
)
from zeroth.service.langgraph_gateway.enforcement_store import LangGraphEnforcementRepository
from zeroth.service.webhooks.models import (
    DeliveryStatus,
    WebhookDelivery,
    WebhookEventType,
    WebhookSubscription,
)
from zeroth.service.webhooks.repository import WebhookRepository

Driver = Callable[[AsyncDatabase], Awaitable[None]]
O = ResourceOperation  # noqa: E741 - compact operation registry alias


def _scope(tenant: str) -> NullWorkspaceScopeContext:
    return NullWorkspaceScopeContext(tenant_id=tenant)


def _run(tenant: str, *, deployment: str = "driver-deployment") -> Run:
    return Run(
        run_id="driver-run",
        thread_id="driver-thread",
        graph_version_ref="driver-graph",
        deployment_ref=deployment,
        tenant_id=tenant,
    )


async def _drive_runs(database: AsyncDatabase, operation: ResourceOperation) -> None:
    owner = RunRepository(database, _scope("driver-owner"))
    foreign = RunRepository(database, _scope("driver-foreign"))
    await owner.create(_run("driver-owner", deployment="owner-deployment"))
    if operation is O.CREATE:
        await foreign.create(_run("driver-foreign", deployment="foreign-deployment"))
        assert (await owner.get("driver-run")).deployment_ref == "owner-deployment"
        assert (await foreign.get("driver-run")).deployment_ref == "foreign-deployment"
    elif operation is O.READ:
        assert await foreign.get("driver-run") is None
        assert await foreign.get("unknown-run") is None
        assert await owner.get("driver-run") is not None
    elif operation is O.ENUMERATE:
        assert [run.run_id for run in await owner.list_runs("owner-deployment")] == ["driver-run"]
        assert await foreign.list_runs("owner-deployment") == []
        assert await foreign.list_runs("unknown-deployment") == []
    elif operation is O.UPDATE:
        with pytest.raises(KeyError):
            await foreign.transition("driver-run", RunStatus.RUNNING)
        with pytest.raises(KeyError):
            await foreign.transition("unknown-run", RunStatus.RUNNING)
        assert (await owner.get("driver-run")).status is RunStatus.PENDING
        assert (await owner.transition("driver-run", RunStatus.RUNNING)).status is RunStatus.RUNNING
    else:
        await foreign.delete("driver-run")
        await foreign.delete("unknown-run")
        assert await owner.get("driver-run") is not None
        await owner.delete("driver-run")
        assert await owner.get("driver-run") is None


def _thread(tenant: str, *, deployment: str) -> Thread:
    return Thread(
        thread_id="driver-thread",
        graph_version_ref="driver-graph",
        deployment_ref=deployment,
        tenant_id=tenant,
    )


async def _drive_threads(database: AsyncDatabase, operation: ResourceOperation) -> None:
    owner = ThreadRepository(database, _scope("driver-owner"))
    foreign = ThreadRepository(database, _scope("driver-foreign"))
    await owner.create(_thread("driver-owner", deployment="owner-deployment"))
    if operation is O.CREATE:
        await foreign.create(_thread("driver-foreign", deployment="foreign-deployment"))
        assert (await owner.get("driver-thread")).deployment_ref == "owner-deployment"
        assert (await foreign.get("driver-thread")).deployment_ref == "foreign-deployment"
    elif operation is O.READ:
        assert await foreign.get("driver-thread") is None
        assert await foreign.get("unknown-thread") is None
    elif operation is O.ENUMERATE:
        assert [thread.thread_id for thread in await owner.list()] == ["driver-thread"]
        assert await foreign.list() == []
    else:
        foreign_thread = await foreign.resolve(
            "driver-thread", graph_version_ref="driver-graph", deployment_ref="foreign-deployment"
        )
        foreign_thread.participating_agent_refs = ["foreign"]
        await foreign.update(foreign_thread)
        assert (await owner.get("driver-thread")).participating_agent_refs == []
        with pytest.raises(KeyError):
            await foreign.attach_run("unknown-thread", "unknown-run")
        owner_thread = await owner.get("driver-thread")
        owner_thread.participating_agent_refs = ["owner"]
        await owner.update(owner_thread)
        assert (await owner.get("driver-thread")).participating_agent_refs == ["owner"]


async def _write_checkpoint(store: CheckpointRowStore, tenant: str, state: str) -> None:
    run = _run(tenant)
    run.workflow_name = state
    await store.write_row(
        checkpoint_id="driver-checkpoint",
        run_id=run.run_id,
        thread_id=run.thread_id,
        checkpoint_order=0,
        state_json=to_json_value(run.model_dump(mode="json")),
        created_at=run.updated_at.isoformat(),
    )


async def _drive_checkpoints(database: AsyncDatabase, operation: ResourceOperation) -> None:
    owner = CheckpointRowStore(database, _scope("driver-owner"))
    foreign = CheckpointRowStore(database, _scope("driver-foreign"))
    await _write_checkpoint(owner, "driver-owner", "owner")
    if operation is O.CREATE:
        await _write_checkpoint(foreign, "driver-foreign", "foreign")
        assert (await owner.get("driver-checkpoint")).workflow_name == "owner"
        assert (await foreign.get("driver-checkpoint")).workflow_name == "foreign"
    elif operation is O.READ:
        assert await foreign.get("driver-checkpoint") is None
        assert await foreign.get("unknown-checkpoint") is None
    elif operation is O.ENUMERATE:
        assert await owner.list_ids("driver-thread") == ["driver-checkpoint"]
        assert await foreign.list_ids("driver-thread") == []
        assert await foreign.list_ids("unknown-thread") == []
    elif operation is O.UPDATE:
        await _write_checkpoint(foreign, "driver-foreign", "foreign")
        await _write_checkpoint(foreign, "driver-foreign", "foreign-updated")
        assert (await owner.get("driver-checkpoint")).workflow_name == "owner"
        await _write_checkpoint(owner, "driver-owner", "owner-updated")
        assert (await owner.get("driver-checkpoint")).workflow_name == "owner-updated"
    else:
        assert not await foreign.delete("driver-checkpoint")
        assert not await foreign.delete("unknown-checkpoint")
        assert await owner.get("driver-checkpoint") is not None
        assert await owner.delete("driver-checkpoint")
        assert await owner.get("driver-checkpoint") is None


def _snapshot(revision: int) -> TokenEngineSnapshot:
    return TokenEngineSnapshot(
        schema_version=1,
        run_id="driver-run",
        revision=revision,
        state=TokenEngineSnapshotState.RUNNING,
        next_token_ordinal=revision + 1,
    )


async def _drive_snapshots(database: AsyncDatabase, operation: ResourceOperation) -> None:
    owner = RunRepository(database, _scope("driver-owner"))
    foreign = RunRepository(database, _scope("driver-foreign"))
    await owner.create(_run("driver-owner"))
    await foreign.create(_run("driver-foreign"))
    await owner.compare_and_swap_token_snapshot(
        "driver-run", expected_revision=None, snapshot=_snapshot(0)
    )
    if operation is O.CREATE:
        await foreign.compare_and_swap_token_snapshot(
            "driver-run", expected_revision=None, snapshot=_snapshot(0)
        )
        assert (await owner.get_token_snapshot("driver-run")).revision == 0
        assert (await foreign.get_token_snapshot("driver-run")).revision == 0
    elif operation is O.READ:
        assert await foreign.get_token_snapshot("unknown-run") is None
        assert (await owner.get_token_snapshot("driver-run")).revision == 0
    elif operation is O.UPDATE:
        await foreign.compare_and_swap_token_snapshot(
            "driver-run", expected_revision=None, snapshot=_snapshot(0)
        )
        await foreign.compare_and_swap_token_snapshot(
            "driver-run", expected_revision=0, snapshot=_snapshot(1)
        )
        assert (await owner.get_token_snapshot("driver-run")).revision == 0
        with pytest.raises(TokenSnapshotConcurrencyError):
            await owner.compare_and_swap_token_snapshot(
                "driver-run", expected_revision=9, snapshot=_snapshot(10)
            )
        await owner.compare_and_swap_token_snapshot(
            "driver-run", expected_revision=0, snapshot=_snapshot(1)
        )
    else:
        async with database.transaction() as connection:
            assert (
                await foreign.erase_token_snapshot_for_run_in_transaction(connection, "driver-run")
                == 0
            )
            assert (
                await foreign.erase_token_snapshot_for_run_in_transaction(connection, "unknown-run")
                == 0
            )
        assert await owner.get_token_snapshot("driver-run") is not None
        async with database.transaction() as connection:
            assert (
                await owner.erase_token_snapshot_for_run_in_transaction(connection, "driver-run")
                == 1
            )
        assert await owner.get_token_snapshot("driver-run") is None


def _subscription(tenant: str) -> WebhookSubscription:
    return WebhookSubscription(
        subscription_id="driver-subscription",
        deployment_ref="driver-deployment",
        tenant_id=tenant,
        target_url=f"https://{tenant}.example.test/hook",
        event_types=[WebhookEventType.RUN_COMPLETED],
    )


async def _drive_webhook_subscriptions(
    database: AsyncDatabase, operation: ResourceOperation
) -> None:
    owner = WebhookRepository(database, _scope("driver-owner"))
    foreign = WebhookRepository(database, _scope("driver-foreign"))
    await owner.create_subscription(_subscription("driver-owner"))
    if operation is O.CREATE:
        await foreign.create_subscription(_subscription("driver-foreign"))
        assert (await owner.get_subscription("driver-subscription")).tenant_id == "driver-owner"
        assert (await foreign.get_subscription("driver-subscription")).tenant_id == "driver-foreign"
    elif operation is O.READ:
        assert await foreign.get_subscription("driver-subscription") is None
        assert await foreign.get_subscription("unknown-subscription") is None
    elif operation is O.ENUMERATE:
        assert len(await owner.list_subscriptions()) == 1
        assert await foreign.list_subscriptions() == []
        assert await foreign.list_subscriptions("unknown-deployment") == []
    elif operation is O.UPDATE:
        await foreign.deactivate_subscription("driver-subscription")
        await foreign.deactivate_subscription("unknown-subscription")
        assert (await owner.get_subscription("driver-subscription")).active
        await owner.deactivate_subscription("driver-subscription")
        assert not (await owner.get_subscription("driver-subscription")).active
    else:
        await foreign.delete_subscription("driver-subscription")
        await foreign.delete_subscription("unknown-subscription")
        assert await owner.get_subscription("driver-subscription") is not None
        await owner.delete_subscription("driver-subscription")
        assert await owner.get_subscription("driver-subscription") is None


async def _delivery_status(database: AsyncDatabase, tenant: str) -> str | None:
    async with database.transaction() as connection:
        row = await connection.fetch_one(
            "SELECT status FROM webhook_deliveries WHERE tenant_id = ? AND delivery_id = ?",
            (tenant, "driver-delivery"),
        )
    return None if row is None else str(row["status"])


async def _seed_delivery(repository: WebhookRepository, tenant: str) -> None:
    await repository.create_subscription(_subscription(tenant))
    await repository.enqueue_delivery(
        WebhookDelivery(
            delivery_id="driver-delivery",
            subscription_id="driver-subscription",
            event_type=WebhookEventType.RUN_COMPLETED,
            payload_json="{}",
            next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    )


async def _drive_webhook_deliveries(database: AsyncDatabase, operation: ResourceOperation) -> None:
    owner = WebhookRepository(database, _scope("driver-owner"))
    foreign = WebhookRepository(database, _scope("driver-foreign"))
    await _seed_delivery(owner, "driver-owner")
    if operation is O.CREATE:
        await _seed_delivery(foreign, "driver-foreign")
        assert await _delivery_status(database, "driver-owner") == DeliveryStatus.PENDING
        assert await _delivery_status(database, "driver-foreign") == DeliveryStatus.PENDING
    elif operation is O.READ:
        assert await foreign.claim_pending_delivery() is None
        assert (
            await WebhookRepository(database, _scope("driver-unknown")).claim_pending_delivery()
            is None
        )
        assert await _delivery_status(database, "driver-owner") == DeliveryStatus.PENDING
    elif operation is O.ENUMERATE:
        assert (await owner.claim_pending_delivery()).delivery_id == "driver-delivery"
        assert await foreign.claim_pending_delivery() is None
        assert (
            await WebhookRepository(database, _scope("driver-unknown")).claim_pending_delivery()
            is None
        )
    else:
        await foreign.mark_delivered("driver-delivery")
        await foreign.mark_delivered("unknown-delivery")
        assert await _delivery_status(database, "driver-owner") == DeliveryStatus.PENDING
        await owner.mark_delivered("driver-delivery")
        assert await _delivery_status(database, "driver-owner") == DeliveryStatus.DELIVERED


async def _drive_webhook_dead_letters(
    database: AsyncDatabase, operation: ResourceOperation
) -> None:
    owner = WebhookRepository(database, _scope("driver-owner"))
    foreign = WebhookRepository(database, _scope("driver-foreign"))
    await _seed_delivery(owner, "driver-owner")
    await owner.dead_letter("driver-delivery")
    owner_rows = await owner.list_dead_letters()
    assert len(owner_rows) == 1
    if operation is O.CREATE:
        # Dead-letter IDs are generated: a foreign append must not change owner state.
        await _seed_delivery(foreign, "driver-foreign")
        await foreign.dead_letter("driver-delivery")
        assert await owner.list_dead_letters() == owner_rows
        foreign_rows = await foreign.list_dead_letters()
        assert len(foreign_rows) == 1
        assert owner_rows[0].delivery_id == foreign_rows[0].delivery_id == "driver-delivery"
        assert owner_rows[0].dead_letter_id != foreign_rows[0].dead_letter_id
        assert await _delivery_status(database, "driver-owner") == DeliveryStatus.DEAD_LETTER
        assert await _delivery_status(database, "driver-foreign") == DeliveryStatus.DEAD_LETTER
    elif operation is O.READ:
        dead_letter_id = owner_rows[0].dead_letter_id
        assert await foreign.get_dead_letter(dead_letter_id) is None
        assert await foreign.get_dead_letter("unknown-dead-letter") is None
    else:
        assert await foreign.list_dead_letters() == []
        assert await WebhookRepository(database, _scope("driver-unknown")).list_dead_letters() == []


async def _drive_policies(database: AsyncDatabase, operation: ResourceOperation) -> None:
    owner = RetentionPolicyRepository(database, _scope("driver-owner"))
    foreign = RetentionPolicyRepository(database, _scope("driver-foreign"))
    await owner.upsert(RetentionPolicy(tenant_id="driver-owner", audit_ttl_seconds=10))
    if operation is O.CREATE:
        await foreign.upsert(RetentionPolicy(tenant_id="driver-foreign", audit_ttl_seconds=20))
        assert (await owner.get()).audit_ttl_seconds == 10
        assert (await foreign.get()).audit_ttl_seconds == 20
    elif operation is O.READ:
        assert await foreign.get() is None
        assert await RetentionPolicyRepository(database, _scope("driver-unknown")).get() is None
        assert (await owner.get()).tenant_id == "driver-owner"
    elif operation is O.ENUMERATE:
        rows = await RetentionPolicyRepository.for_privileged_tenant_maintenance(
            database
        ).list_all_enabled_for_maintenance()
        assert "driver-owner" in {row.tenant_id for row in rows}
    else:
        await foreign.upsert(RetentionPolicy(tenant_id="driver-foreign", audit_ttl_seconds=20))
        assert (await owner.get()).audit_ttl_seconds == 10
        with pytest.raises(ValueError):
            await foreign.upsert(RetentionPolicy(tenant_id="driver-unknown"))
        await owner.upsert(RetentionPolicy(tenant_id="driver-owner", audit_ttl_seconds=30))
        assert (await owner.get()).audit_ttl_seconds == 30


async def _drive_legal_holds(database: AsyncDatabase, operation: ResourceOperation) -> None:
    owner = LegalHoldRepository(database, _scope("driver-owner"))
    foreign = LegalHoldRepository(database, _scope("driver-foreign"))
    hold = await owner.place(run_id="driver-run")
    if operation is O.CREATE:
        await foreign.place(run_id="driver-run")
        assert len(await owner.list_for_tenant()) == 1
        assert len(await foreign.list_for_tenant()) == 1
    elif operation is O.READ:
        assert await foreign.get(hold.hold_id) is None
        assert await foreign.get("unknown-hold") is None
    elif operation is O.ENUMERATE:
        assert len(await owner.list_for_tenant()) == 1
        assert await foreign.list_for_tenant() == []
        assert (await foreign.active_holds_for_tenant()).run_ids == set()
    else:
        assert not await foreign.release(hold.hold_id)
        assert not await foreign.release("unknown-hold")
        assert (await owner.get(hold.hold_id)).active
        assert await owner.release(hold.hold_id)
        assert not (await owner.get(hold.hold_id)).active


async def _drive_retention_audit(database: AsyncDatabase, operation: ResourceOperation) -> None:
    owner = RetentionAuditLogRepository(database, _scope("driver-owner"))
    foreign = RetentionAuditLogRepository(database, _scope("driver-foreign"))
    log_id = await owner.record(action="driver", run_id="driver-run")
    if operation is O.CREATE:
        await foreign.record(action="foreign", run_id="driver-run")
        assert len(await owner.list_for_run("driver-run")) == 1
        assert len(await foreign.list_for_run("driver-run")) == 1
    elif operation is O.READ:
        assert await foreign.get(log_id) is None
        assert await foreign.get("unknown-log") is None
    else:
        assert len(await owner.list_for_tenant()) == 1
        assert await foreign.list_for_tenant() == []
        assert await foreign.list_for_run("unknown-run") == []


def _manifest(tenant: str) -> CleanupManifest:
    run_id = "driver-run"
    return CleanupManifest(
        tenant_id=tenant,
        run_id=run_id,
        reason="rte",
        database_result=DatabaseErasureOutcome(),
        operations=[
            CleanupOperation(
                operation_id=operation_id(tenant, run_id, "artifact_prefix", run_id),
                kind="artifact_prefix",
                tenant_id=tenant,
                run_id=run_id,
            ),
            CleanupOperation(
                operation_id=operation_id(tenant, run_id, "econ", run_id),
                kind="econ",
                tenant_id=tenant,
                run_id=run_id,
                join_keys=[run_id],
            ),
        ],
    )


def _authorization_id(tenant: str) -> str:
    # Authorization log IDs are generated globally; isolation is observed by
    # foreign access to the owner's ID and independent foreign state.
    return f"driver-authorization-{tenant}"


async def _initialize_cleanup(
    database: AsyncDatabase, repository: CleanupStateRepository, tenant: str
) -> None:
    async with database.transaction() as connection:
        await connection.execute(
            """
            INSERT INTO retention_audit_log (
                tenant_id, log_id, run_id, action, reason, detail, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tenant,
                _authorization_id(tenant),
                "driver-run",
                "authorized",
                "rte",
                None,
                datetime.now(UTC).isoformat(),
            ),
        )
        await repository.initialize_in_transaction(
            connection,
            authorization_log_id=_authorization_id(tenant),
            manifest=_manifest(tenant),
        )


async def _drive_cleanup_state(database: AsyncDatabase, operation: ResourceOperation) -> None:
    owner = CleanupStateRepository(database, _scope("driver-owner"))
    foreign = CleanupStateRepository(database, _scope("driver-foreign"))
    await _initialize_cleanup(database, owner, "driver-owner")
    if operation is O.CREATE:
        await _initialize_cleanup(database, foreign, "driver-foreign")
        async with database.transaction() as connection:
            assert (
                await owner.get_state_in_transaction(connection, _authorization_id("driver-owner"))
            ).tenant_id == "driver-owner"
            assert (
                await foreign.get_state_in_transaction(
                    connection, _authorization_id("driver-foreign")
                )
            ).tenant_id == "driver-foreign"
    elif operation is O.READ:
        async with database.transaction() as connection:
            assert (
                await foreign.get_state_in_transaction(
                    connection, _authorization_id("driver-owner")
                )
                is None
            )
            assert (
                await foreign.get_state_in_transaction(connection, "unknown-authorization") is None
            )
    else:
        await _initialize_cleanup(database, foreign, "driver-foreign")
        lease = datetime.now(UTC) + timedelta(minutes=1)
        async with database.transaction() as connection:
            await foreign.claim_in_transaction(
                connection,
                authorization_log_id=_authorization_id("driver-foreign"),
                expected_generation=0,
                expected_revision=0,
                claim_id="foreign-claim",
                claim_log_id="foreign-log",
                lease_expires_at=lease,
            )
            assert (
                await owner.get_state_in_transaction(connection, _authorization_id("driver-owner"))
            ).revision == 0
            with pytest.raises(ValueError, match="disappeared"):
                await owner.claim_in_transaction(
                    connection,
                    authorization_log_id="unknown-authorization",
                    expected_generation=0,
                    expected_revision=0,
                    claim_id="unknown",
                    claim_log_id="unknown",
                    lease_expires_at=lease,
                )
            updated = await owner.claim_in_transaction(
                connection,
                authorization_log_id=_authorization_id("driver-owner"),
                expected_generation=0,
                expected_revision=0,
                claim_id="owner-claim",
                claim_log_id="owner-log",
                lease_expires_at=lease,
            )
            assert updated.revision == 1


async def _drive_cleanup_operations(database: AsyncDatabase, operation: ResourceOperation) -> None:
    owner = CleanupStateRepository(database, _scope("driver-owner"))
    foreign = CleanupStateRepository(database, _scope("driver-foreign"))
    await _initialize_cleanup(database, owner, "driver-owner")
    owner_operation = _manifest("driver-owner").operations[0]
    if operation is O.CREATE:
        await _initialize_cleanup(database, foreign, "driver-foreign")
        async with database.transaction() as connection:
            assert (
                await owner.get_operation_in_transaction(
                    connection, _authorization_id("driver-owner"), owner_operation.operation_id
                )
                is not None
            )
    elif operation is O.READ:
        async with database.transaction() as connection:
            assert (
                await foreign.get_operation_in_transaction(
                    connection, _authorization_id("driver-owner"), owner_operation.operation_id
                )
                is None
            )
            assert (
                await foreign.get_operation_in_transaction(
                    connection, "unknown-authorization", "unknown-operation"
                )
                is None
            )
    elif operation is O.ENUMERATE:
        async with database.transaction() as connection:
            assert (
                len(
                    await owner.list_operations_in_transaction(
                        connection, _authorization_id("driver-owner")
                    )
                )
                == 2
            )
            assert (
                await foreign.list_operations_in_transaction(
                    connection, _authorization_id("driver-owner")
                )
                == []
            )
            assert (
                await foreign.list_operations_in_transaction(connection, "unknown-authorization")
                == []
            )
    else:
        await _initialize_cleanup(database, foreign, "driver-foreign")
        lease = datetime.now(UTC) + timedelta(minutes=1)
        foreign_operation = _manifest("driver-foreign").operations[0]
        async with database.transaction() as connection:
            with pytest.raises(ValueError, match="unknown operation"):
                await foreign.update_operation_in_transaction(
                    connection,
                    authorization_log_id=_authorization_id("driver-foreign"),
                    claim_id="unknown",
                    generation=0,
                    expected_revision=0,
                    operation=owner_operation,
                    lease_expires_at=lease,
                )
            with pytest.raises(ValueError, match="unknown operation"):
                await foreign.update_operation_in_transaction(
                    connection,
                    authorization_log_id="unknown-authorization",
                    claim_id="unknown",
                    generation=0,
                    expected_revision=0,
                    operation=foreign_operation,
                    lease_expires_at=lease,
                )
            assert (
                await owner.get_operation_in_transaction(
                    connection, _authorization_id("driver-owner"), owner_operation.operation_id
                )
            ).status == "pending"
            claimed = await foreign.claim_in_transaction(
                connection,
                authorization_log_id=_authorization_id("driver-foreign"),
                expected_generation=0,
                expected_revision=0,
                claim_id="foreign-claim",
                claim_log_id="foreign-claim-log",
                lease_expires_at=lease,
            )
            updated_operation = foreign_operation.model_copy(update={"status": "completed"})
            updated_state = await foreign.update_operation_in_transaction(
                connection,
                authorization_log_id=_authorization_id("driver-foreign"),
                claim_id="foreign-claim",
                generation=claimed.generation,
                expected_revision=claimed.revision,
                operation=updated_operation,
                lease_expires_at=lease,
            )
            assert updated_state.revision == claimed.revision + 1
            assert (
                await foreign.get_operation_in_transaction(
                    connection,
                    _authorization_id("driver-foreign"),
                    foreign_operation.operation_id,
                )
            ).status == "completed"
            assert (
                await owner.get_operation_in_transaction(
                    connection, _authorization_id("driver-owner"), owner_operation.operation_id
                )
            ).status == "pending"


async def _drive_coordination(database: AsyncDatabase, operation: ResourceOperation) -> None:
    owner = RetentionCoordinator(database, _scope("driver-owner"))
    foreign = RetentionCoordinator(database, _scope("driver-foreign"))
    async with owner.transaction() as transaction:
        assert transaction.tenant_id == "driver-owner"
    async with foreign.transaction() as transaction:
        assert transaction.tenant_id == "driver-foreign"
    async with owner.transaction() as transaction:
        assert transaction.tenant_id == "driver-owner"
    assert operation in {O.CREATE, O.READ}


def _decision_response() -> DecisionResponseV1:
    return DecisionResponseV1(
        decision_id="driver-decision",
        idempotency_key="driver-key",
        decision=ToolDecisionKind.ALLOW,
        reason_code="allowed",
        policy_version="driver-policy",
    )


async def _drive_decisions(database: AsyncDatabase, operation: ResourceOperation) -> None:
    owner = LangGraphEnforcementRepository(database, _scope("driver-owner"))
    foreign = LangGraphEnforcementRepository(database, _scope("driver-foreign"))
    await owner.save_decision("driver-key", "driver-deployment", "owner-hash", _decision_response())
    if operation is O.CREATE:
        await foreign.save_decision(
            "driver-key", "driver-deployment", "foreign-hash", _decision_response()
        )
        assert await owner.count_decisions() == 1
        assert await foreign.count_decisions() == 1
    elif operation is O.READ:
        stored = await owner.save_decision(
            "driver-key", "driver-deployment", "owner-hash", _decision_response()
        )
        assert stored.decision_id == "driver-decision"
        assert await foreign.count_decisions() == 0
    else:
        assert await owner.count_decisions() == 1
        assert await foreign.count_decisions() == 0
        assert (
            await LangGraphEnforcementRepository(
                database, _scope("driver-unknown")
            ).count_decisions()
            == 0
        )


def _inventory(
    tenant: str, *, coverage: InventoryCoverage = InventoryCoverage.COMPLETE
) -> InventoryRegistrationV1:
    entries = (InventoryEntryV1(name="driver-tool", fingerprint="sha256:driver"),)
    return InventoryRegistrationV1(
        context_token="unused",
        tenant_id=tenant,
        principal_id="driver-principal",
        deployment_ref="driver-deployment",
        graph_version="driver-graph",
        coverage=coverage,
        entries=entries,
        inventory_fingerprint=inventory_fingerprint(entries),
    )


async def _drive_inventories(database: AsyncDatabase, operation: ResourceOperation) -> None:
    owner = LangGraphEnforcementRepository(database, _scope("driver-owner"))
    foreign = LangGraphEnforcementRepository(database, _scope("driver-foreign"))
    owner_request = _inventory("driver-owner")
    await owner.register_inventory(owner_request)
    identity = (
        owner_request.deployment_ref,
        owner_request.graph_version,
        owner_request.adapter_version,
        owner_request.inventory_fingerprint,
    )
    if operation is O.CREATE:
        await foreign.register_inventory(_inventory("driver-foreign"))
        assert (await owner.get_inventory(*identity))["tenant_id"] == "driver-owner"
        assert (await foreign.get_inventory(*identity))["tenant_id"] == "driver-foreign"
    elif operation is O.READ:
        assert await foreign.get_inventory(*identity) is None
        assert await foreign.get_inventory("unknown", *identity[1:]) is None
    else:
        await foreign.register_inventory(_inventory("driver-foreign"))
        await foreign.register_inventory(
            _inventory("driver-foreign", coverage=InventoryCoverage.PARTIAL)
        )
        assert (await owner.get_inventory(*identity))[
            "coverage"
        ] == InventoryCoverage.COMPLETE.value
        with pytest.raises(ValueError):
            await foreign.register_inventory(_inventory("driver-unknown"))
        await owner.register_inventory(
            _inventory("driver-owner", coverage=InventoryCoverage.PARTIAL)
        )
        assert (await owner.get_inventory(*identity))["coverage"] == InventoryCoverage.PARTIAL.value


def _attestation(tenant: str) -> dict[str, object]:
    return {
        "tenant_id": tenant,
        "deployment_ref": "driver-deployment",
        "run_id": "driver-run",
        "correlation_id": "driver-correlation",
        "governance_level": "enforced",
        "observed_at": "2026-08-12T00:00:00+00:00",
        "graph_version": "driver-graph",
        "adapter_version": "driver-adapter",
        "inventory_fingerprint": "driver-fingerprint",
        "tool_manifest_complete": True,
    }


async def _drive_attestations(database: AsyncDatabase, operation: ResourceOperation) -> None:
    owner = LangGraphEnforcementRepository(database, _scope("driver-owner"))
    foreign = LangGraphEnforcementRepository(database, _scope("driver-foreign"))
    await owner.save_attestation(_attestation("driver-owner"), b"owner", "key", "hmac")
    if operation is O.CREATE:
        await foreign.save_attestation(_attestation("driver-foreign"), b"foreign", "key", "hmac")
        assert (await owner.get_attestation_by_run_id("driver-deployment", "driver-run"))[
            "tenant_id"
        ] == "driver-owner"
        assert (await foreign.get_attestation_by_run_id("driver-deployment", "driver-run"))[
            "tenant_id"
        ] == "driver-foreign"
    elif operation is O.READ:
        assert await foreign.get_attestation_by_run_id("driver-deployment", "driver-run") is None
        assert await foreign.get_attestation_by_run_id("driver-deployment", "unknown-run") is None
    else:
        with pytest.warns(DeprecationWarning):
            assert await foreign.get_attestation("driver-deployment", "driver-correlation") is None
        with pytest.warns(DeprecationWarning):
            assert await foreign.get_attestation("driver-deployment", "unknown-correlation") is None


async def _drive_side_effect_operations(
    database: AsyncDatabase,
    operation: ResourceOperation,
) -> None:
    owner = SideEffectOperationStore(database, _scope("driver-owner"))
    foreign = SideEffectOperationStore(database, _scope("driver-foreign"))
    claim = {
        "run_id": "driver-run",
        "dispatch_id": "driver-dispatch",
        "idempotency_key": "driver-idempotency",
        "target_ref": "unit://driver",
    }
    await owner.claim("driver-operation", **claim)
    if operation is O.CREATE:
        assert (await foreign.claim("driver-operation", **claim)).first_execution
        assert await owner.get("driver-operation") is not None
        assert await foreign.get("driver-operation") is not None
    elif operation is O.READ:
        assert await foreign.get("driver-operation") is None
        assert await foreign.state_of("driver-operation") is OperationState.NOT_STARTED
        assert await foreign.get("unknown-operation") is None
        assert await owner.get("driver-operation") is not None
    elif operation is O.ENUMERATE:
        await owner.mark_ambiguous("driver-operation", reason="driver")
        assert await foreign.pending_reconciliation("driver-run") == []
        assert await foreign.pending_reconciliation("unknown-run") == []
        assert len(await owner.pending_reconciliation("driver-run")) == 1
    elif operation is O.UPDATE:
        assert await foreign.complete("driver-operation", receipt="foreign") is False
        await foreign.fail("driver-operation", error="foreign")
        await foreign.mark_ambiguous("driver-operation", reason="foreign")
        assert (
            await foreign.record_reconciliation(
                "driver-operation",
                resolved=True,
                receipt="foreign",
            )
            is OperationState.NOT_STARTED
        )
        assert (await owner.get("driver-operation"))["state"] == OperationState.IN_FLIGHT
    else:
        assert await foreign.erase_for_run("driver-run") == 0
        assert await foreign.erase_for_run("unknown-run") == 0
        assert await owner.get("driver-operation") is not None
        assert await owner.erase_for_run("driver-run") == 1


_RESOURCE_DRIVERS: dict[str, Callable[[AsyncDatabase, ResourceOperation], Awaitable[None]]] = {
    "service.runs": _drive_runs,
    "service.threads": _drive_threads,
    "service.run_checkpoints": _drive_checkpoints,
    "service.token_engine_snapshots": _drive_snapshots,
    "service.side_effect_operations": _drive_side_effect_operations,
    "service.webhook_subscriptions": _drive_webhook_subscriptions,
    "service.webhook_deliveries": _drive_webhook_deliveries,
    "service.webhook_dead_letters": _drive_webhook_dead_letters,
    "service.retention_policies": _drive_policies,
    "service.legal_holds": _drive_legal_holds,
    "service.retention_audit_log": _drive_retention_audit,
    "service.retention_cleanup_state": _drive_cleanup_state,
    "service.retention_cleanup_operations": _drive_cleanup_operations,
    "service.retention_coordination": _drive_coordination,
    "service.langgraph_decisions": _drive_decisions,
    "service.langgraph_inventories": _drive_inventories,
    "service.langgraph_run_attestations": _drive_attestations,
}

_OPERATIONS = {
    "service.runs": frozenset(O),
    "service.threads": frozenset({O.CREATE, O.READ, O.ENUMERATE, O.UPDATE}),
    "service.run_checkpoints": frozenset(O),
    "service.token_engine_snapshots": frozenset({O.CREATE, O.READ, O.UPDATE, O.DELETE}),
    "service.side_effect_operations": frozenset(O),
    "service.webhook_subscriptions": frozenset(O),
    "service.webhook_deliveries": frozenset({O.CREATE, O.READ, O.ENUMERATE, O.UPDATE}),
    "service.webhook_dead_letters": frozenset({O.CREATE, O.READ, O.ENUMERATE}),
    "service.retention_policies": frozenset({O.CREATE, O.READ, O.ENUMERATE, O.UPDATE}),
    "service.legal_holds": frozenset({O.CREATE, O.READ, O.ENUMERATE, O.UPDATE}),
    "service.retention_audit_log": frozenset({O.CREATE, O.READ, O.ENUMERATE}),
    "service.retention_cleanup_state": frozenset({O.CREATE, O.READ, O.UPDATE}),
    "service.retention_cleanup_operations": frozenset({O.CREATE, O.READ, O.ENUMERATE, O.UPDATE}),
    "service.retention_coordination": frozenset({O.CREATE, O.READ}),
    "service.langgraph_decisions": frozenset({O.CREATE, O.READ, O.ENUMERATE}),
    "service.langgraph_inventories": frozenset({O.CREATE, O.READ, O.UPDATE}),
    "service.langgraph_run_attestations": frozenset({O.CREATE, O.READ, O.ENUMERATE}),
}

TASK9_EXECUTABLE_DRIVERS: dict[tuple[str, ResourceOperation], Driver] = {
    (resource_name, operation): partial(_RESOURCE_DRIVERS[resource_name], operation=operation)
    for resource_name, operations in _OPERATIONS.items()
    for operation in operations
}
