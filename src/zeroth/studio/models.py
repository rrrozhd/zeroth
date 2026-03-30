"""Shared Studio authoring models."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from zeroth.graph.models import Graph


def _utc_now() -> datetime:
    return datetime.now(UTC)


class WorkflowRecord(BaseModel):
    """Stored workflow metadata owned by a tenant and workspace."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    tenant_id: str
    workspace_id: str
    graph_id: str
    name: str
    folder_path: str = "/"
    archived_at: datetime | None = None
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)


class WorkflowDraftHead(BaseModel):
    """Current mutable draft pointer for a workflow."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    tenant_id: str
    workspace_id: str
    draft_graph_version: int = Field(ge=1)
    revision_token: str
    validation_status: str
    last_saved_at: datetime = Field(default_factory=_utc_now)


class WorkflowSummary(BaseModel):
    """Workflow metadata returned by scoped list operations."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    tenant_id: str
    workspace_id: str
    graph_id: str
    name: str
    folder_path: str
    draft_graph_version: int = Field(ge=1)
    validation_status: str
    updated_at: datetime
    last_saved_at: datetime


class WorkflowDetail(BaseModel):
    """Workflow metadata plus the current draft graph."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    tenant_id: str
    workspace_id: str
    graph_id: str
    name: str
    folder_path: str
    draft_graph_version: int = Field(ge=1)
    revision_token: str
    validation_status: str
    updated_at: datetime
    last_saved_at: datetime
    graph: Graph


class WorkflowLease(BaseModel):
    """Exclusive edit lease for a workflow draft."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    tenant_id: str
    workspace_id: str
    lease_token: str
    subject: str
    acquired_at: datetime
    expires_at: datetime


class WorkflowLeaseConflict(BaseModel):
    """Lease conflict payload returned when another editor holds the draft."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    tenant_id: str
    workspace_id: str
    lease_token: str
    subject: str
    expires_at: datetime
