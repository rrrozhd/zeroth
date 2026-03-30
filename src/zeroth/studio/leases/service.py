"""Workflow lease service with scope validation."""

from __future__ import annotations

from zeroth.studio.leases.repository import WorkflowLeaseRepository
from zeroth.studio.models import WorkflowLease, WorkflowLeaseConflict
from zeroth.studio.workflows.repository import WorkflowRepository


class WorkflowLeaseService:
    """Acquire, renew, and release workflow leases within workspace scope."""

    def __init__(
        self,
        *,
        workflow_repository: WorkflowRepository,
        lease_repository: WorkflowLeaseRepository | None = None,
    ) -> None:
        self._workflow_repository = workflow_repository
        self._lease_repository = lease_repository or WorkflowLeaseRepository(
            workflow_repository.database
        )

    def acquire_lease(
        self,
        *,
        workflow_id: str,
        tenant_id: str,
        workspace_id: str,
        subject: str,
        ttl_seconds: int = 30,
    ) -> WorkflowLease | WorkflowLeaseConflict | None:
        """Acquire an edit lease if the workflow exists inside the requested scope."""
        if not self._workflow_repository.has_workflow(workflow_id, tenant_id, workspace_id):
            return None
        return self._lease_repository.acquire_lease(
            workflow_id=workflow_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            subject=subject,
            ttl_seconds=ttl_seconds,
        )

    def renew_lease(
        self,
        *,
        workflow_id: str,
        tenant_id: str,
        workspace_id: str,
        lease_token: str,
        ttl_seconds: int = 30,
    ) -> WorkflowLease | None:
        """Renew a lease only when the workflow exists in the same scope."""
        if not self._workflow_repository.has_workflow(workflow_id, tenant_id, workspace_id):
            return None
        return self._lease_repository.renew_lease(
            workflow_id=workflow_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            lease_token=lease_token,
            ttl_seconds=ttl_seconds,
        )

    def release_lease(
        self,
        *,
        workflow_id: str,
        tenant_id: str,
        workspace_id: str,
        lease_token: str,
    ) -> bool:
        """Release a lease only when the workflow exists in the same scope."""
        if not self._workflow_repository.has_workflow(workflow_id, tenant_id, workspace_id):
            return False
        return self._lease_repository.release_lease(
            workflow_id=workflow_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            lease_token=lease_token,
        )
