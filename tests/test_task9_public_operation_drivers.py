"""Direct production-repository drivers for Task 9 operation edges."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from zeroth.governance.retention.cleanup_manifest import CleanupOperation
from zeroth.governance.retention.cleanup_state_repository import CleanupStateRepository
from zeroth.governance.retention.models import RetentionPolicy
from zeroth.governance.retention.policy_repository import RetentionPolicyRepository
from zeroth.platform.storage import NullWorkspaceScopeContext
from zeroth.service.webhooks.models import WebhookEventType, WebhookSubscription
from zeroth.service.webhooks.repository import WebhookRepository
from tests.task9_operation_driver_registry import TASK9_EXECUTABLE_DRIVERS


@pytest.mark.parametrize(
    ("resource_name", "operation"),
    sorted(TASK9_EXECUTABLE_DRIVERS, key=lambda item: (item[0], item[1].value)),
)
async def test_task9_public_operation_isolation_driver(
    async_database, resource_name, operation
) -> None:
    await TASK9_EXECUTABLE_DRIVERS[(resource_name, operation)](async_database)


async def test_webhook_foreign_delete_is_unknown_and_owner_survives(async_database) -> None:
    owner = WebhookRepository(async_database, NullWorkspaceScopeContext(tenant_id="driver-owner"))
    foreign = WebhookRepository(
        async_database, NullWorkspaceScopeContext(tenant_id="driver-foreign")
    )
    await owner.create_subscription(
        WebhookSubscription(
            subscription_id="driver-sub",
            tenant_id="driver-owner",
            deployment_ref="driver-deployment",
            target_url="https://example.test/hook",
            event_types=[WebhookEventType.RUN_COMPLETED],
        )
    )

    await foreign.delete_subscription("driver-sub")

    assert await foreign.get_subscription("driver-sub") is None
    assert await owner.get_subscription("driver-sub") is not None


async def test_privileged_policy_enumeration_reads_both_tenants(async_database) -> None:
    for tenant_id in ("driver-policy-a", "driver-policy-b"):
        repository = RetentionPolicyRepository(
            async_database, NullWorkspaceScopeContext(tenant_id=tenant_id)
        )
        await repository.upsert(RetentionPolicy(tenant_id=tenant_id, enabled=True))

    maintenance = RetentionPolicyRepository.for_privileged_tenant_maintenance(async_database)
    policies = await maintenance.list_all_enabled_for_maintenance()

    assert {policy.tenant_id for policy in policies} >= {
        "driver-policy-a",
        "driver-policy-b",
    }


async def test_cleanup_unknown_operation_update_preserves_owner_state(async_database) -> None:
    repository = CleanupStateRepository(
        async_database, NullWorkspaceScopeContext.for_default_compatibility()
    )
    operation = CleanupOperation(
        operation_id="driver-missing-operation",
        kind="artifact_key",
        tenant_id="default",
        run_id="driver-run",
        artifact_key="driver/key",
        status="in_progress",
    )
    async with async_database.transaction() as connection:
        assert (
            await repository.get_operation_in_transaction(
                connection, "driver-missing-authorization", operation.operation_id
            )
            is None
        )
        with pytest.raises(ValueError, match="unknown operation"):
            await repository.update_operation_in_transaction(
                connection,
                authorization_log_id="driver-missing-authorization",
                claim_id="driver-claim",
                generation=1,
                expected_revision=0,
                operation=operation,
                lease_expires_at=datetime.now(UTC),
            )
