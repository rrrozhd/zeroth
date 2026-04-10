"""Deployment models for immutable published graph snapshots."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def _utc_now() -> datetime:
    """Return the current time in UTC."""
    return datetime.now(UTC)


class DeploymentStatus(StrEnum):
    """Lifecycle status for immutable deployment versions."""

    ACTIVE = "active"
    SUPERSEDED = "superseded"


class Deployment(BaseModel):
    """A persisted deployment snapshot for a published graph version."""

    model_config = ConfigDict(extra="forbid")

    deployment_id: str
    deployment_ref: str
    version: int = Field(default=1, ge=1)
    graph_id: str
    graph_version: int = Field(ge=1)
    graph_version_ref: str
    serialized_graph: str
    entry_input_contract_ref: str | None = None
    entry_input_contract_version: int | None = Field(default=None, ge=1)
    entry_output_contract_ref: str | None = None
    entry_output_contract_version: int | None = Field(default=None, ge=1)
    deployment_settings_snapshot: dict[str, Any] = Field(default_factory=dict)
    graph_snapshot_digest: str = ""
    contract_snapshot_digest: str = ""
    settings_snapshot_digest: str = ""
    attestation_digest: str = ""
    tenant_id: str = "default"
    workspace_id: str | None = None
    status: DeploymentStatus = DeploymentStatus.ACTIVE
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)
