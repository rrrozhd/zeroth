"""Persistence models for repo checkouts and repo runs (ZER-37).

A :class:`RepoCheckout` row is the durable record of one staged repository
checkout: which installation and repository it came from, the pinned commit
and tree identities the pipeline verified, its lifecycle state, and the
attestation envelope recorded once staging succeeds. A :class:`RepoRun` row is
one script execution against a staged checkout, claimed and fenced by durable
workers using the same generation pattern the webhook delivery worker uses.

Payloads are bounded here, at the model boundary, so an oversized input or
output is refused before it ever reaches a SQL parameter.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from zeroth.integrations.github.models import CheckoutFailureCode
from zeroth.platform.primitives import utc_now

INPUT_PAYLOAD_CAP_BYTES = 256 * 1024
"""Upper bound on a repo run's input payload (UTF-8 bytes)."""

OUTPUT_PAYLOAD_CAP_BYTES = 1024 * 1024
"""Upper bound on a repo run's recorded output payload (UTF-8 bytes)."""


def ensure_bounded_payload(value: str | None, *, cap: int, field_name: str) -> str | None:
    """Refuse a payload whose UTF-8 encoding exceeds ``cap`` bytes."""
    if value is not None and len(value.encode("utf-8")) > cap:
        raise ValueError(f"{field_name} exceeds the {cap}-byte cap")
    return value


class RepoCheckoutState(StrEnum):
    """Lifecycle of one persisted repository checkout."""

    REQUESTED = "requested"
    VERIFYING = "verifying"
    FETCHING = "fetching"
    HARDENING = "hardening"
    STAGED = "staged"
    CONSUMED = "consumed"
    EXPIRED = "expired"
    FAILED = "failed"


class RepoCheckout(BaseModel):
    """One durable checkout record, from request through staging to consumption.

    ``workspace_id`` is nullable per the repo convention for workspace-scoped
    rows (mirroring runs and deployments): a row created outside any workspace
    stores ``NULL`` and is only visible through a null-workspace scope.
    ``failure_detail`` is ALWAYS pre-redacted by callers before it reaches this
    model. The attestation columns mirror the deployment attestation column
    set; ``None`` throughout means no attestation has been recorded yet.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid4().hex)
    tenant_id: str = "default"
    workspace_id: str | None = None
    repository_pk: str
    installation_id: int
    repository_id: int
    repository_full_name: str
    requested_ref: str
    resolved_commit_sha: str | None = None
    git_tree_id: str | None = None
    tree_digest: str | None = None
    config_digest: str | None = None
    manifest_digest: str | None = None
    script_name: str | None = None
    state: RepoCheckoutState = RepoCheckoutState.REQUESTED
    failure_code: CheckoutFailureCode | None = None
    failure_detail: str | None = None
    staged_path: str | None = None
    file_count: int | None = None
    size_bytes: int | None = None
    has_lfs_pointers: bool | None = None
    cache_hit: bool = False
    verified_at: datetime | None = None
    expires_at: datetime | None = None
    attestation_digest: str | None = None
    attestation_signature: str | None = None
    attestation_key_id: str | None = None
    attestation_algorithm: str | None = None
    attestation_payload_json: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class RepoRunState(StrEnum):
    """Lifecycle of one script execution against a staged checkout."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RepoRun(BaseModel):
    """One durable repo-run record with worker lease and generation fencing.

    The lease fields mirror the webhook delivery worker's house pattern
    (``attempt_count`` + ``next_attempt_at``) under explicit names:
    ``claim_generation`` is the fencing generation a claim increments, and
    ``lease_expires_at`` doubles as the due horizon -- a ``PENDING`` row is
    claimable once it passes, and a ``RUNNING`` row whose lease lapsed belongs
    to a dead worker and may be re-claimed at the next generation. When left
    ``None`` the repository stamps it with ``created_at`` (due immediately).
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid4().hex)
    tenant_id: str = "default"
    workspace_id: str | None = None
    checkout_id: str
    script_name: str
    input_payload_json: str | None = None
    state: RepoRunState = RepoRunState.PENDING
    exit_code: int | None = None
    failure_code: str | None = None
    smoke_passed: bool | None = None
    output_payload_json: str | None = None
    claimed_by: str | None = None
    claim_generation: int = Field(default=0, ge=0)
    claimed_at: datetime | None = None
    lease_expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("input_payload_json")
    @classmethod
    def _bounded_input(cls, value: str | None) -> str | None:
        """Refuse an input payload over :data:`INPUT_PAYLOAD_CAP_BYTES`."""
        return ensure_bounded_payload(
            value, cap=INPUT_PAYLOAD_CAP_BYTES, field_name="input_payload_json"
        )

    @field_validator("output_payload_json")
    @classmethod
    def _bounded_output(cls, value: str | None) -> str | None:
        """Refuse an output payload over :data:`OUTPUT_PAYLOAD_CAP_BYTES`."""
        return ensure_bounded_payload(
            value, cap=OUTPUT_PAYLOAD_CAP_BYTES, field_name="output_payload_json"
        )


__all__ = [
    "INPUT_PAYLOAD_CAP_BYTES",
    "OUTPUT_PAYLOAD_CAP_BYTES",
    "RepoCheckout",
    "RepoCheckoutState",
    "RepoRun",
    "RepoRunState",
    "ensure_bounded_payload",
]
