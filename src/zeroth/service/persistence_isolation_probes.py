"""Production-owned executable isolation probes for scoped repositories.

Each case calls a production repository. Generated-ID append-only resources
prove isolation by showing that a foreign append does not alter the owner's
enumeration; collision-capable resources use the same logical identifier.
"""

from __future__ import annotations

import re
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel

from zeroth.contracts.graph import Graph, TokenEngineSnapshot, TokenEngineSnapshotState
from zeroth.contracts.graph.repository import GraphRepository
from zeroth.contracts.registry.errors import ContractNotFoundError
from zeroth.contracts.registry.registry import ContractRegistry
from zeroth.governance.approvals.models import ApprovalRecord, ApprovalStatus
from zeroth.governance.approvals.repository import ApprovalRepository
from zeroth.governance.attestations.heartbeat import Heartbeat, HeartbeatRepository
from zeroth.governance.attestations.payload import RunAttestationPayload, SignedRunAttestation
from zeroth.governance.attestations.store import (
    InventoryRegistration,
    InventoryRegistrationRepository,
    RunAttestationRepository,
)
from zeroth.governance.decisions.repository import DecisionRepository
from zeroth.governance.decisions.request import (
    DecisionKind,
    DecisionRequest,
    DecisionVerdict,
    NormalizedAction,
)
from zeroth.governance.guardrails.rate_limit import QuotaEnforcer, TokenBucketRateLimiter
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
from zeroth.integrations.github.models import (
    InstallationState,
    RepositoryGrant,
    RepositoryState,
)
from zeroth.integrations.langgraph import InventoryCoverage, ToolDecisionKind
from zeroth.integrations.memory.config_repository import MemoryConnectorConfigRepository
from zeroth.integrations.persistence.runs.checkpoint_store import CheckpointRowStore
from zeroth.integrations.persistence.runs.run_repository import RunRepository
from zeroth.integrations.persistence.runs.thread_repository import ThreadRepository
from zeroth.platform.dispatch.operations import OperationState, SideEffectOperationStore
from zeroth.platform.storage import (
    AsyncDatabase,
    NullWorkspaceScopeContext,
    ResourceOperation,
    ScopeContext,
)
from zeroth.platform.storage.json import to_json_value
from zeroth.runtime.orchestration.token_snapshot_store import TokenSnapshotConcurrencyError
from zeroth.runtime.runs import Run, RunStatus, Thread
from zeroth.service.deployments.models import Deployment
from zeroth.service.deployments.repository import SQLiteDeploymentRepository
from zeroth.service.github.repository import SQLiteGitHubRepository
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

O = ResourceOperation  # noqa: E741 - compact operation registry alias


@contextmanager
def _raises(error_type: type[BaseException], match: str | None = None) -> Iterator[None]:
    """Build raises data for the tenant-isolation probe."""
    try:
        yield
    except error_type as error:
        if match is not None and re.search(match, str(error)) is None:
            raise AssertionError(f"exception did not match {match!r}") from error
    else:
        raise AssertionError(f"expected {error_type.__name__}")


@contextmanager
def _warns(warning_type: type[Warning]) -> Iterator[None]:
    """Build warns data for the tenant-isolation probe."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        yield
    if not any(issubclass(item.category, warning_type) for item in caught):
        raise AssertionError(f"expected {warning_type.__name__}")


def _scope(tenant: str) -> NullWorkspaceScopeContext:
    """Build scope data for the tenant-isolation probe."""
    return NullWorkspaceScopeContext(tenant_id=tenant)


def _run(tenant: str, *, deployment: str = "driver-deployment") -> Run:
    """Build run data for the tenant-isolation probe."""
    return Run(
        run_id="driver-run",
        thread_id="driver-thread",
        graph_version_ref="driver-graph",
        deployment_ref=deployment,
        tenant_id=tenant,
    )


async def _drive_runs(database: AsyncDatabase, operation: ResourceOperation) -> None:
    """Exercise runs operations through the tenant-isolation matrix."""
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
        named = RunRepository(
            database, ScopeContext(tenant_id="driver-owner", workspace_id="driver-workspace")
        )
        run = _run("driver-owner")
        run.run_id = "driver-workspace-run"
        run.thread_id = "driver-workspace-thread"
        run.workspace_id = "driver-workspace"
        await named.create(run)
    elif operation is O.UPDATE:
        with _raises(KeyError):
            await foreign.transition("driver-run", RunStatus.RUNNING)
        with _raises(KeyError):
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
    """Build thread data for the tenant-isolation probe."""
    return Thread(
        thread_id="driver-thread",
        graph_version_ref="driver-graph",
        deployment_ref=deployment,
        tenant_id=tenant,
    )


async def _drive_threads(database: AsyncDatabase, operation: ResourceOperation) -> None:
    """Exercise threads operations through the tenant-isolation matrix."""
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
        with _raises(KeyError):
            await foreign.attach_run("unknown-thread", "unknown-run")
        owner_thread = await owner.get("driver-thread")
        owner_thread.participating_agent_refs = ["owner"]
        await owner.update(owner_thread)
        assert (await owner.get("driver-thread")).participating_agent_refs == ["owner"]


async def _write_checkpoint(store: CheckpointRowStore, tenant: str, state: str) -> None:
    """Write checkpoint through its scoped repository."""
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
    """Exercise checkpoints operations through the tenant-isolation matrix."""
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
    """Build snapshot data for the tenant-isolation probe."""
    return TokenEngineSnapshot(
        schema_version=1,
        run_id="driver-run",
        revision=revision,
        state=TokenEngineSnapshotState.RUNNING,
        next_token_ordinal=revision + 1,
    )


async def _drive_snapshots(database: AsyncDatabase, operation: ResourceOperation) -> None:
    """Exercise snapshots operations through the tenant-isolation matrix."""
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
        with _raises(TokenSnapshotConcurrencyError):
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
    """Build subscription data for the tenant-isolation probe."""
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
    """Exercise webhook subscriptions operations through the tenant-isolation matrix."""
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
    """Build delivery status data for the tenant-isolation probe."""
    async with database.transaction() as connection:
        row = await connection.fetch_one(
            "SELECT status FROM webhook_deliveries WHERE tenant_id = ? AND delivery_id = ?",
            (tenant, "driver-delivery"),
        )
    return None if row is None else str(row["status"])


async def _seed_delivery(repository: WebhookRepository, tenant: str) -> None:
    """Seed delivery for the isolation probe."""
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
    """Exercise webhook deliveries operations through the tenant-isolation matrix."""
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
        claim = await owner.claim_pending_delivery()
        assert claim is not None and claim.delivery.delivery_id == "driver-delivery"
        assert await foreign.claim_pending_delivery() is None
        assert (
            await WebhookRepository(database, _scope("driver-unknown")).claim_pending_delivery()
            is None
        )
    else:
        await foreign.mark_delivered("driver-delivery", 1)
        await foreign.mark_delivered("unknown-delivery", 1)
        assert await _delivery_status(database, "driver-owner") == DeliveryStatus.PENDING
        claim = await owner.claim_pending_delivery()
        assert claim is not None
        await owner.mark_delivered("driver-delivery", claim.generation)
        assert await _delivery_status(database, "driver-owner") == DeliveryStatus.DELIVERED


async def _drive_webhook_dead_letters(
    database: AsyncDatabase, operation: ResourceOperation
) -> None:
    """Exercise webhook dead letters operations through the tenant-isolation matrix."""
    owner = WebhookRepository(database, _scope("driver-owner"))
    foreign = WebhookRepository(database, _scope("driver-foreign"))
    await _seed_delivery(owner, "driver-owner")
    owner_claim = await owner.claim_pending_delivery()
    assert owner_claim is not None
    await owner.dead_letter("driver-delivery", owner_claim.generation)
    owner_rows = await owner.list_dead_letters()
    assert len(owner_rows) == 1
    if operation is O.CREATE:
        # Dead-letter IDs are generated: a foreign append must not change owner state.
        await _seed_delivery(foreign, "driver-foreign")
        foreign_claim = await foreign.claim_pending_delivery()
        assert foreign_claim is not None
        await foreign.dead_letter("driver-delivery", foreign_claim.generation)
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
    """Exercise policies operations through the tenant-isolation matrix."""
    owner = RetentionPolicyRepository.scoped(database, _scope("driver-owner"))
    foreign = RetentionPolicyRepository.scoped(database, _scope("driver-foreign"))
    await owner.upsert(RetentionPolicy(tenant_id="driver-owner", audit_ttl_seconds=10))
    if operation is O.CREATE:
        await foreign.upsert(RetentionPolicy(tenant_id="driver-foreign", audit_ttl_seconds=20))
        assert (await owner.get()).audit_ttl_seconds == 10
        assert (await foreign.get()).audit_ttl_seconds == 20
    elif operation is O.READ:
        assert await foreign.get() is None
        assert (
            await RetentionPolicyRepository.scoped(database, _scope("driver-unknown")).get() is None
        )
        assert (await owner.get()).tenant_id == "driver-owner"
    elif operation is O.ENUMERATE:
        assert [row.tenant_id for row in await owner.list_for_tenant()] == ["driver-owner"]
        assert await foreign.list_for_tenant() == []
        assert (
            await RetentionPolicyRepository.scoped(
                database, _scope("driver-unknown")
            ).list_for_tenant()
            == []
        )
    else:
        await foreign.upsert(RetentionPolicy(tenant_id="driver-foreign", audit_ttl_seconds=20))
        assert (await owner.get()).audit_ttl_seconds == 10
        with _raises(ValueError):
            await foreign.upsert(RetentionPolicy(tenant_id="driver-unknown"))
        await owner.upsert(RetentionPolicy(tenant_id="driver-owner", audit_ttl_seconds=30))
        assert (await owner.get()).audit_ttl_seconds == 30


async def _drive_legal_holds(database: AsyncDatabase, operation: ResourceOperation) -> None:
    """Exercise legal holds operations through the tenant-isolation matrix."""
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
    """Exercise retention audit operations through the tenant-isolation matrix."""
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
    """Build manifest data for the tenant-isolation probe."""
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
    """Build authorization id data for the tenant-isolation probe."""
    return f"driver-authorization-{tenant}"


async def _initialize_cleanup(
    database: AsyncDatabase, repository: CleanupStateRepository, tenant: str
) -> None:
    """Initialize cleanup for the isolation probe."""
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
    """Exercise cleanup state operations through the tenant-isolation matrix."""
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
            with _raises(ValueError, match="disappeared"):
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
    """Exercise cleanup operations operations through the tenant-isolation matrix."""
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
            with _raises(ValueError, match="unknown operation"):
                await foreign.update_operation_in_transaction(
                    connection,
                    authorization_log_id=_authorization_id("driver-foreign"),
                    claim_id="unknown",
                    generation=0,
                    expected_revision=0,
                    operation=owner_operation,
                    lease_expires_at=lease,
                )
            with _raises(ValueError, match="unknown operation"):
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
    """Exercise coordination operations through the tenant-isolation matrix."""
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
    """Build decision response data for the tenant-isolation probe."""
    return DecisionResponseV1(
        decision_id="driver-decision",
        idempotency_key="driver-key",
        decision=ToolDecisionKind.ALLOW,
        reason_code="allowed",
        policy_version="driver-policy",
    )


async def _drive_decisions(database: AsyncDatabase, operation: ResourceOperation) -> None:
    """Exercise decisions operations through the tenant-isolation matrix."""
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
    """Build inventory data for the tenant-isolation probe."""
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
    """Exercise inventories operations through the tenant-isolation matrix."""
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
        with _raises(ValueError):
            await foreign.register_inventory(_inventory("driver-unknown"))
        await owner.register_inventory(
            _inventory("driver-owner", coverage=InventoryCoverage.PARTIAL)
        )
        assert (await owner.get_inventory(*identity))["coverage"] == InventoryCoverage.PARTIAL.value


def _attestation(tenant: str) -> dict[str, object]:
    """Build attestation data for the tenant-isolation probe."""
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
    """Exercise attestations operations through the tenant-isolation matrix."""
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
        with _warns(DeprecationWarning):
            assert await foreign.get_attestation("driver-deployment", "driver-correlation") is None
        with _warns(DeprecationWarning):
            assert await foreign.get_attestation("driver-deployment", "unknown-correlation") is None


async def _drive_side_effect_operations(
    database: AsyncDatabase,
    operation: ResourceOperation,
) -> None:
    """Exercise side effect operations operations through the tenant-isolation matrix."""
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


async def _drive_rate_limit_buckets(database: AsyncDatabase, operation: ResourceOperation) -> None:
    """Exercise rate limit buckets operations through the tenant-isolation matrix."""
    owner = TokenBucketRateLimiter.scoped(database, _scope("driver-owner"))
    foreign = TokenBucketRateLimiter.scoped(database, _scope("driver-foreign"))
    await owner.check_and_consume("driver-key", capacity=1, refill_rate=0)
    if operation is O.CREATE:
        assert await foreign.check_and_consume("driver-key", capacity=1, refill_rate=0)
    elif operation is O.READ:
        assert await foreign.get("driver-key") is None
        assert await foreign.get("unknown-key") is None
    else:
        assert not await owner.check_and_consume("driver-key", capacity=1, refill_rate=0)
        assert await foreign.check_and_consume("driver-key", capacity=2, refill_rate=0)
        assert (await owner.get("driver-key"))["token_count"] == 0


async def _drive_quota_counters(database: AsyncDatabase, operation: ResourceOperation) -> None:
    """Exercise quota counters operations through the tenant-isolation matrix."""
    owner = QuotaEnforcer.scoped(database, _scope("driver-owner"))
    foreign = QuotaEnforcer.scoped(database, _scope("driver-foreign"))
    assert await owner.check_and_increment("driver-key", limit=1)
    if operation is O.CREATE:
        assert await foreign.check_and_increment("driver-key", limit=1)
    elif operation is O.READ:
        assert await foreign.get("driver-key") is None
        assert await foreign.get("unknown-key") is None
    else:
        assert not await owner.check_and_increment("driver-key", limit=1)
        assert await foreign.check_and_increment("driver-key", limit=2)
        assert (await owner.get("driver-key"))["value"] == 1


class _DriverContract(BaseModel):
    """Represent the DriverContract contract used by isolation probes."""
    value: str


async def _drive_contract_versions(database: AsyncDatabase, operation: ResourceOperation) -> None:
    """Exercise contract versions operations through the tenant-isolation matrix."""
    owner = ContractRegistry.scoped(database, _scope("driver-owner"))
    foreign = ContractRegistry.scoped(database, _scope("driver-foreign"))
    await owner.register(_DriverContract, name="driver-contract", version=1)
    if operation is O.CREATE:
        await foreign.register(_DriverContract, name="driver-contract", version=1)
        assert (await owner.get("driver-contract", 1)).name == "driver-contract"
    elif operation is O.READ:
        with _raises(ContractNotFoundError):
            await foreign.get("driver-contract", 1)
        with _raises(ContractNotFoundError):
            await foreign.get("unknown-contract", 1)
    elif operation is O.ENUMERATE:
        assert await foreign.list_names() == []
        assert await owner.list_names() == ["driver-contract"]
    else:
        await foreign.delete("driver-contract", 1)
        await foreign.delete("unknown-contract", 1)
        assert (await owner.get("driver-contract", 1)).name == "driver-contract"


def _approval(tenant: str, approval_id: str = "driver-approval") -> ApprovalRecord:
    """Build approval data for the tenant-isolation probe."""
    return ApprovalRecord(
        approval_id=approval_id,
        run_id="driver-run",
        node_id="driver-node",
        graph_version_ref="driver-graph",
        deployment_ref="driver-deployment",
        tenant_id=tenant,
        summary="driver",
        rationale="driver",
    )


async def _drive_approvals(database: AsyncDatabase, operation: ResourceOperation) -> None:
    """Exercise approvals operations through the tenant-isolation matrix."""
    repository = ApprovalRepository(database)
    owner = _approval("driver-owner")
    await repository.write(owner)
    if operation is O.CREATE:
        with _raises(KeyError):
            await repository.write(_approval("driver-foreign"))
    elif operation is O.READ:
        assert await repository.get(owner.approval_id, tenant_id="driver-foreign") is None
        assert await repository.get("unknown-approval", tenant_id="driver-foreign") is None
    elif operation is O.ENUMERATE:
        assert await repository.list(tenant_id="driver-foreign") == []
    else:
        foreign_update = owner.model_copy(
            update={"tenant_id": "driver-foreign", "status": ApprovalStatus.RESOLVED}
        )
        assert await repository.resolve_pending(foreign_update) is None
    owner_after = await repository.get(owner.approval_id, tenant_id="driver-owner")
    assert owner_after is not None
    assert owner_after.tenant_id == "driver-owner"
    assert owner_after.status is ApprovalStatus.PENDING
    assert await repository.get(owner.approval_id, tenant_id="driver-foreign") is None
    assert await repository.get("unknown-approval", tenant_id="driver-foreign") is None


def _decision_request(tenant: str) -> DecisionRequest:
    """Build decision request data for the tenant-isolation probe."""
    return DecisionRequest(
        tenant_id=tenant,
        principal_id="driver-principal",
        deployment_ref="driver-deployment",
        action=NormalizedAction(
            name="driver-tool", fingerprint="driver-fingerprint", arguments_digest="driver-args"
        ),
        idempotency_key="driver-key",
    )


async def _drive_decision_records(database: AsyncDatabase, operation: ResourceOperation) -> None:
    """Exercise decision records operations through the tenant-isolation matrix."""
    repository = DecisionRepository(database)
    request = _decision_request("driver-owner")
    await repository.record(
        request,
        digest="driver-digest",
        verdict=DecisionVerdict(
            kind=DecisionKind.ALLOW, reason_code="driver", policy_version="driver-policy"
        ),
    )
    if operation is O.CREATE:
        await repository.record(
            _decision_request("driver-foreign"),
            digest="driver-digest",
            verdict=DecisionVerdict(
                kind=DecisionKind.DENY, reason_code="driver", policy_version="driver-policy"
            ),
        )
    else:
        assert await repository.find_by_idempotency_key("driver-foreign", "driver-key") is None
        assert await repository.find_by_idempotency_key("driver-foreign", "unknown-key") is None
    owner_after = await repository.find_by_idempotency_key("driver-owner", "driver-key")
    assert owner_after is not None
    assert owner_after.response.tenant_id == "driver-owner"
    foreign_after = await repository.find_by_idempotency_key("driver-foreign", "driver-key")
    if operation is O.CREATE:
        assert foreign_after is not None
        assert foreign_after.response.tenant_id == "driver-foreign"
    else:
        assert foreign_after is None


def _deployment(tenant: str, *, version: int = 1, ref: str = "driver-deployment") -> Deployment:
    """Build deployment data for the tenant-isolation probe."""
    return Deployment(
        deployment_id=f"{tenant}-{version}",
        deployment_ref=ref,
        version=version,
        graph_id="driver-graph",
        graph_version=1,
        graph_version_ref="driver-graph@1",
        serialized_graph="{}",
        tenant_id=tenant,
    )


async def _drive_deployments(database: AsyncDatabase, operation: ResourceOperation) -> None:
    """Exercise deployments operations through the tenant-isolation matrix."""
    repository = SQLiteDeploymentRepository(database)
    await repository.create(_deployment("driver-owner"), tenant_id="driver-owner")
    if operation in {O.CREATE, O.UPDATE}:
        await repository.create(
            _deployment("driver-foreign", version=2),
            tenant_id="driver-foreign",
        )
    elif operation is O.READ:
        assert await repository.get("driver-deployment", tenant_id="driver-foreign") is None
        assert await repository.get("unknown-deployment", tenant_id="driver-foreign") is None
    elif operation is O.ENUMERATE:
        assert await repository.list(tenant_id="driver-foreign") == []
    owner_after = await repository.get("driver-deployment", tenant_id="driver-owner")
    assert owner_after is not None
    assert owner_after.status.value == "active"
    foreign_after = await repository.get("driver-deployment", tenant_id="driver-foreign")
    if operation in {O.CREATE, O.UPDATE}:
        assert foreign_after is not None
        assert foreign_after.tenant_id == "driver-foreign"
    else:
        assert foreign_after is None
    assert await repository.get("unknown-deployment", tenant_id="driver-foreign") is None


async def _drive_heartbeats(database: AsyncDatabase, operation: ResourceOperation) -> None:
    """Exercise heartbeats operations through the tenant-isolation matrix."""
    repository = HeartbeatRepository(database)
    owner_created = await repository.record(
        Heartbeat(
            tenant_id="driver-owner",
            deployment_ref="driver-deployment",
            reported_level="observed",
        )
    )
    assert owner_created.tenant_id == "driver-owner"
    if operation is O.CREATE:
        foreign_created = await repository.record(
            Heartbeat(
                tenant_id="driver-foreign",
                deployment_ref="driver-deployment",
                reported_level="unknown",
            )
        )
        assert foreign_created.tenant_id == "driver-foreign"
    else:
        assert await repository.latest_for_deployment("driver-foreign", "driver-deployment") is None
        assert (
            await repository.latest_for_deployment("driver-foreign", "unknown-deployment") is None
        )
    owner_after = await repository.latest_for_deployment("driver-owner", "driver-deployment")
    assert owner_after is not None
    assert owner_after.tenant_id == "driver-owner"
    foreign_after = await repository.latest_for_deployment("driver-foreign", "driver-deployment")
    if operation is O.CREATE:
        assert foreign_after is not None
        assert foreign_after.tenant_id == "driver-foreign"
    else:
        assert foreign_after is None


def _graph(tenant: str, graph_id: str = "driver-graph") -> Graph:
    """Build graph data for the tenant-isolation probe."""
    return Graph(graph_id=graph_id, name="driver", tenant_id=tenant)


async def _drive_graph_versions(database: AsyncDatabase, operation: ResourceOperation) -> None:
    """Exercise graph versions operations through the tenant-isolation matrix."""
    repository = GraphRepository(database)
    await repository.create(_graph("driver-owner"), tenant_id="driver-owner")
    if operation is O.CREATE:
        with _raises(Exception):
            await repository.create(_graph("driver-foreign"), tenant_id="driver-foreign")
    elif operation is O.READ:
        assert await repository.get("driver-graph", tenant_id="driver-foreign") is None
        assert await repository.get("unknown-graph", tenant_id="driver-foreign") is None
    elif operation is O.ENUMERATE:
        assert await repository.list(tenant_id="driver-foreign") == []
    else:
        with _raises(Exception):
            await repository.save(
                _graph("driver-foreign").model_copy(update={"name": "driver-updated"}),
                tenant_id="driver-foreign",
            )
    owner_after = await repository.get("driver-graph", tenant_id="driver-owner")
    assert owner_after is not None
    assert owner_after.name == "driver"
    assert await repository.get("driver-graph", tenant_id="driver-foreign") is None
    assert await repository.get("unknown-graph", tenant_id="driver-foreign") is None


async def _drive_memory_configs(database: AsyncDatabase, operation: ResourceOperation) -> None:
    """Exercise memory configs operations through the tenant-isolation matrix."""
    repository = MemoryConnectorConfigRepository(database)
    await repository.upsert("driver-owner-ref", "memory", {}, tenant_id="driver-owner")
    if operation is O.CREATE:
        with _raises(KeyError):
            await repository.upsert("driver-owner-ref", "memory", {}, tenant_id="driver-foreign")
    elif operation is O.READ:
        assert await repository.get("driver-owner-ref", tenant_id="driver-foreign") is None
        assert await repository.get("unknown-ref", tenant_id="driver-foreign") is None
    elif operation is O.ENUMERATE:
        assert await repository.list(tenant_id="driver-foreign") == []
    elif operation is O.UPDATE:
        await repository.upsert("driver-foreign-ref", "memory", {}, tenant_id="driver-foreign")
        await repository.upsert(
            "driver-foreign-ref", "memory", {"foreign": True}, tenant_id="driver-foreign"
        )
    else:
        assert not await repository.delete("driver-owner-ref", tenant_id="driver-foreign")
        assert not await repository.delete("unknown-ref", tenant_id="driver-foreign")
    owner_after = await repository.get("driver-owner-ref", tenant_id="driver-owner")
    assert owner_after is not None
    assert owner_after.params == {}
    assert await repository.get("driver-owner-ref", tenant_id="driver-foreign") is None
    assert await repository.get("unknown-ref", tenant_id="driver-foreign") is None


def _signed_attestation(tenant: str) -> SignedRunAttestation:
    """Build signed attestation data for the tenant-isolation probe."""
    now = datetime.now(UTC)
    return SignedRunAttestation(
        payload=RunAttestationPayload(
            correlation_id="driver-correlation",
            tenant_id=tenant,
            deployment_ref="driver-deployment",
            graph_version="driver-graph",
            adapter_version="driver-adapter",
            inventory_fingerprint="driver-fingerprint",
            inventory_coverage="complete",
            tool_count=0,
            claimed_level="observed",
            issued_at=now,
            expires_at=now + timedelta(minutes=5),
        ),
        digest=f"driver-{tenant}",
    )


async def _drive_run_attestations(database: AsyncDatabase, operation: ResourceOperation) -> None:
    """Exercise run attestations operations through the tenant-isolation matrix."""
    repository = RunAttestationRepository(database)
    await repository.record(_signed_attestation("driver-owner"))
    if operation is O.CREATE:
        await repository.record(_signed_attestation("driver-foreign"))
    else:
        assert await repository.find_by_correlation("driver-foreign", "driver-correlation") is None
        assert await repository.find_by_correlation("driver-foreign", "unknown-correlation") is None
    owner_after = await repository.find_by_correlation("driver-owner", "driver-correlation")
    assert owner_after is not None
    assert owner_after.payload.tenant_id == "driver-owner"
    foreign_after = await repository.find_by_correlation("driver-foreign", "driver-correlation")
    if operation is O.CREATE:
        assert foreign_after is not None
        assert foreign_after.payload.tenant_id == "driver-foreign"
    else:
        assert foreign_after is None


async def _seed_github_installation(
    repository: SQLiteGitHubRepository, tenant: str
) -> None:
    """Seed one installation row for the tenant-isolation probe."""
    await repository.upsert_installation(
        tenant,
        installation_id=4242,
        account_login=f"{tenant}-account",
        account_type="Organization",
        repository_selection="selected",
    )


async def _drive_github_installations(
    database: AsyncDatabase, operation: ResourceOperation
) -> None:
    """Exercise github installations operations through the tenant-isolation matrix."""
    repository = SQLiteGitHubRepository(database)
    await _seed_github_installation(repository, "driver-owner")
    if operation is O.CREATE:
        # The same GitHub installation id in another tenant is a distinct row.
        await _seed_github_installation(repository, "driver-foreign")
        owner = await repository.get_installation("driver-owner", 4242)
        foreign = await repository.get_installation("driver-foreign", 4242)
        assert owner is not None and owner.account_login == "driver-owner-account"
        assert foreign is not None and foreign.account_login == "driver-foreign-account"
    elif operation is O.READ:
        assert await repository.get_installation("driver-foreign", 4242) is None
        assert await repository.get_installation("driver-foreign", 9999) is None
        assert await repository.get_installation("driver-owner", 4242) is not None
    elif operation is O.ENUMERATE:
        owned = await repository.list_installations("driver-owner")
        assert [row.installation_id for row in owned] == [4242]
        assert await repository.list_installations("driver-foreign") == []
    else:
        await repository.set_installation_status(
            "driver-foreign", 4242, InstallationState.SUSPENDED
        )
        await repository.set_installation_status(
            "driver-foreign", 9999, InstallationState.SUSPENDED
        )
        owner = await repository.get_installation("driver-owner", 4242)
        assert owner is not None
        assert owner.status is InstallationState.PENDING_CLAIM
        await repository.set_installation_status("driver-owner", 4242, InstallationState.ACTIVE)
        owner = await repository.get_installation("driver-owner", 4242)
        assert owner is not None and owner.status is InstallationState.ACTIVE


def _github_grant(tenant: str) -> RepositoryGrant:
    """Build a repository grant for the tenant-isolation probe."""
    return RepositoryGrant(
        repo_id=1001,
        owner=f"{tenant}-account",
        name="driver-repo",
        full_name=f"{tenant}-account/driver-repo",
        private=True,
        default_branch="main",
    )


async def _seed_github_repository(repository: SQLiteGitHubRepository, tenant: str) -> str:
    """Seed one installation plus one grant; return the installation row id."""
    installation = await repository.upsert_installation(
        tenant,
        installation_id=4242,
        account_login=f"{tenant}-account",
        account_type="Organization",
        repository_selection="selected",
    )
    await repository.upsert_repository(
        tenant, installation_pk=installation.id, grant=_github_grant(tenant)
    )
    return installation.id


async def _drive_github_repositories(
    database: AsyncDatabase, operation: ResourceOperation
) -> None:
    """Exercise github repositories operations through the tenant-isolation matrix."""
    repository = SQLiteGitHubRepository(database)
    owner_pk = await _seed_github_repository(repository, "driver-owner")
    if operation is O.CREATE:
        # The same repo id under another tenant is a distinct scoped row.
        foreign_pk = await _seed_github_repository(repository, "driver-foreign")
        owner = await repository.get_repository("driver-owner", owner_pk, 1001)
        foreign = await repository.get_repository("driver-foreign", foreign_pk, 1001)
        assert owner is not None and owner.owner == "driver-owner-account"
        assert foreign is not None and foreign.owner == "driver-foreign-account"
    elif operation is O.READ:
        assert await repository.get_repository("driver-foreign", owner_pk, 1001) is None
        assert await repository.get_repository("driver-foreign", "unknown-pk", 1001) is None
        assert await repository.get_repository("driver-owner", owner_pk, 1001) is not None
    elif operation is O.ENUMERATE:
        assert len(await repository.list_repositories("driver-owner", owner_pk)) == 1
        assert await repository.list_repositories("driver-foreign", owner_pk) == []
        assert await repository.list_repositories("driver-foreign", "unknown-pk") == []
    else:
        await repository.set_repository_status(
            "driver-foreign", installation_pk=owner_pk, status=RepositoryState.REMOVED
        )
        await repository.set_repository_status(
            "driver-foreign", installation_pk="unknown-pk", status=RepositoryState.REMOVED
        )
        owner = await repository.get_repository("driver-owner", owner_pk, 1001)
        assert owner is not None and owner.status is RepositoryState.ACTIVE
        await repository.set_repository_status(
            "driver-owner", installation_pk=owner_pk, status=RepositoryState.REMOVED
        )
        owner = await repository.get_repository("driver-owner", owner_pk, 1001)
        assert owner is not None and owner.status is RepositoryState.REMOVED


async def _drive_github_webhook_deliveries(
    database: AsyncDatabase, operation: ResourceOperation
) -> None:
    """Exercise github webhook deliveries operations through the tenant-isolation matrix."""
    repository = SQLiteGitHubRepository(database)
    assert await repository.record_delivery(
        "driver-owner", "driver-guid", event="installation", action="created",
        installation_id=4242,
    )
    horizon = datetime.now(UTC) + timedelta(days=1)
    if operation is O.CREATE:
        # A duplicate GUID is refused within a tenant but not across tenants.
        assert not await repository.record_delivery(
            "driver-owner", "driver-guid", event="installation", action="created",
            installation_id=4242,
        )
        assert await repository.record_delivery(
            "driver-foreign", "driver-guid", event="installation", action="created",
            installation_id=4242,
        )
    elif operation is O.ENUMERATE:
        assert await repository.prune_deliveries("driver-foreign", horizon) == 0
        assert await repository.prune_deliveries("driver-unknown", horizon) == 0
        # The owner's row survived both foreign sweeps: its GUID still dedups.
        assert not await repository.record_delivery(
            "driver-owner", "driver-guid", event="installation", action="created",
            installation_id=4242,
        )
    else:
        assert await repository.prune_deliveries("driver-foreign", horizon) == 0
        assert not await repository.record_delivery(
            "driver-owner", "driver-guid", event="installation", action="created",
            installation_id=4242,
        )
        assert await repository.prune_deliveries("driver-owner", horizon) == 1
        assert await repository.record_delivery(
            "driver-owner", "driver-guid", event="installation", action="created",
            installation_id=4242,
        )


def _inventory_registration(tenant: str) -> InventoryRegistration:
    """Build inventory registration data for the tenant-isolation probe."""
    return InventoryRegistration(
        tenant_id=tenant,
        deployment_ref="driver-deployment",
        graph_version="driver-graph",
        adapter_version="driver-adapter",
        coverage="complete",
    )


async def _drive_tool_inventories(database: AsyncDatabase, operation: ResourceOperation) -> None:
    """Exercise tool inventories operations through the tenant-isolation matrix."""
    repository = InventoryRegistrationRepository(database)
    owner_created = await repository.register(_inventory_registration("driver-owner"))
    assert owner_created.tenant_id == "driver-owner"
    if operation is O.CREATE:
        foreign_created = await repository.register(_inventory_registration("driver-foreign"))
        assert foreign_created.tenant_id == "driver-foreign"
    else:
        assert await repository.latest_for_deployment("driver-foreign", "driver-deployment") is None
        assert (
            await repository.latest_for_deployment("driver-foreign", "unknown-deployment") is None
        )
    owner_after = await repository.latest_for_deployment("driver-owner", "driver-deployment")
    assert owner_after is not None
    assert owner_after.tenant_id == "driver-owner"
    foreign_after = await repository.latest_for_deployment("driver-foreign", "driver-deployment")
    if operation is O.CREATE:
        assert foreign_after is not None
        assert foreign_after.tenant_id == "driver-foreign"
    else:
        assert foreign_after is None
