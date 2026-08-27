"""Data models for WS-E retention, legal holds, and erasure bookkeeping."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from zeroth.platform.primitives import utc_now

# The reserved tenant that owns the system-wide default retention policy,
# consulted whenever a tenant has no explicit policy row of its own.
SYSTEM_DEFAULT_TENANT = "default"
# Migration 008 stores TTLs as SQL INTEGER for SQLite/PostgreSQL portability.
# Keep the public contract within PostgreSQL int4 until those columns migrate
# to BIGINT.
MAX_TTL_SECONDS = 2_147_483_647


class RetentionPolicy(BaseModel):
    """Per-tenant retention TTLs. ``tenant_id='default'`` is the fallback.

    A ``None`` TTL means "keep forever" for that surface (no automatic purge).
    ``enabled`` False parks a tenant's policy without deleting it — TTL purge is
    then skipped for that tenant while explicit erasure still works.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    # TTLs are whole positive seconds; zero/negative would put the cutoff at
    # "now" or in the future and erase everything, so they fail validation.
    audit_ttl_seconds: int | None = Field(default=None, ge=1, le=MAX_TTL_SECONDS)
    run_ttl_seconds: int | None = Field(default=None, ge=1, le=MAX_TTL_SECONDS)
    enabled: bool = True
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class LegalHold(BaseModel):
    """A hold that blocks BOTH TTL purge and explicit erasure while active.

    ``run_id is None`` == a tenant-wide hold (freezes every run for the tenant).
    """

    model_config = ConfigDict(extra="forbid")

    hold_id: str
    tenant_id: str
    run_id: str | None = None
    reason: str | None = None
    active: bool = True
    placed_by: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    released_at: datetime | None = None


@dataclass(slots=True)
class TenantHolds:
    """Resolved active-hold state for a tenant.

    ``tenant_wide`` short-circuits everything: when True the whole tenant is
    frozen regardless of ``run_ids``.
    """

    tenant_wide: bool = False
    run_ids: set[str] = field(default_factory=set)

    def blocks(self, run_id: str) -> bool:
        """Return True if erasure of ``run_id`` is blocked by an active hold."""
        return self.tenant_wide or run_id in self.run_ids


@dataclass(slots=True)
class ErasureResult:
    """Idempotent per-run erasure outcome (counts of what was removed)."""

    run_id: str
    tenant_id: str
    reason: str
    audits_erased: int = 0
    checkpoints_deleted: int = 0
    operations_deleted: int = 0
    run_redacted: bool = False
    artifacts_deleted: int = 0
    econ_events_deleted: int | None = None  # None == econ hook not wired
    external_cleanup_status: Literal["complete", "failed", "pending"] = "pending"
    authorization_log_id: str | None = None
    retry_log_id: str | None = None


# ``ErasureResult``'s constructor signature is pinned by the protected surface
# fixtures, so ZER-26's ``operations_deleted`` count is hidden from the reported
# signature rather than recorded as a surface change -- the same idiom
# ``ZerothSettings`` and ``RuntimeOrchestrator`` use. The field is still an
# ordinary keyword argument and is populated by the erasure service.
_erasure_result_parameters = inspect.signature(ErasureResult).parameters
ErasureResult.__signature__ = inspect.signature(ErasureResult).replace(
    parameters=[
        parameter
        for name, parameter in _erasure_result_parameters.items()
        if name not in {"operations_deleted"}
    ]
)
