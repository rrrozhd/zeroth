"""Deployment models for immutable published graph snapshots."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from zeroth.platform.primitives import utc_now


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
    # WS-D keyed signature over ``attestation_digest``. Nullable so legacy rows
    # (deployed before signing existed) hydrate as unsigned-legacy rather than
    # signed-invalid. See docs/provenance-trust-model.md.
    attestation_signature: str | None = None
    attestation_signing_key_id: str | None = None
    attestation_algorithm: str | None = None
    tenant_id: str = "default"
    workspace_id: str | None = None
    status: DeploymentStatus = DeploymentStatus.ACTIVE
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
