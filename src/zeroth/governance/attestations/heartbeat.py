"""Deployment-level liveness from unsigned heartbeats.

``enforcement_heartbeats`` (migration 016) is append-only, last-known-wins
liveness evidence: an SDK adapter posts one row per interval, and the newest
row for a deployment is its current status. This is a materially weaker
evidence kind than :mod:`zeroth.governance.attestations.store` -- a heartbeat
carries no signature and no bound tool inventory, only a self-reported
``reported_level`` and a timestamp.

That weakness is deliberate and load-bearing:

**R14 -- a heartbeat can never upgrade an individual run.**
``CapabilityReporter.level_for_run`` already discards the
``deployment_evidence`` parameter outright (see
``zeroth.governance.langgraph_gateway.capabilities:103``); this module does not
touch that path and relies on it being structural, not re-derived here.

**R13 -- a stale heartbeat downgrades the deployment-level last-known
status.** :class:`DeploymentStatusResolver` builds
:class:`~zeroth.contracts.langgraph_gateway.models.RunCapabilityEvidence` with
``observed_at`` taken verbatim from the heartbeat, so
``CapabilityReporter._validated_level`` applies its own
``0 <= age <= stale_after_seconds`` window -- the staleness rule lives in one
place, not two.

**A heartbeat's claim is a ceiling, never a grant.** ``tool_manifest_complete``
is always ``False`` here, because a heartbeat proves no inventory. That alone
stops ``_validated_level`` from ever reaching its ``ENFORCED`` branch (which
requires ``governance_level is ENFORCED`` **and** ``tool_manifest_complete``),
so a heartbeat claiming ``"enforced"`` degrades to ``OBSERVED`` -- the same
ceiling-then-clamp shape as ``claimed_level`` in
:mod:`zeroth.governance.attestations.provider`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from zeroth.contracts.langgraph_gateway.models import GovernanceLevel, RunCapabilityEvidence
from zeroth.platform.config.settings import LangGraphGatewaySettings
from zeroth.platform.primitives import utc_now
from zeroth.platform.storage import AsyncDatabase, ResourceOperation
from zeroth.platform.storage.scoping import (
    named_isolation_probe,
    persistence_operation,
    persistence_surface,
)

_HEARTBEAT_COLUMNS = (
    "heartbeat_id, tenant_id, deployment_ref, graph_version, adapter_version, "
    "observed_at, reported_level, created_at"
)

_SELECT_LATEST_HEARTBEAT = (
    f"SELECT {_HEARTBEAT_COLUMNS} FROM enforcement_heartbeats "
    "WHERE tenant_id = ? AND deployment_ref = ? "
    "ORDER BY observed_at DESC LIMIT 1"
)

DEFAULT_STALE_AFTER_SECONDS: float = float(
    LangGraphGatewaySettings.model_fields["stale_threshold_seconds"].get_default()
)
"""Default freshness window, sourced from ``stale_threshold_seconds``.

Read from :class:`LangGraphGatewaySettings` rather than duplicated as a
literal, so this module and the gateway settings can never drift apart on
what "stale" means.
"""

_GOVERNANCE_LEVEL_BY_VALUE: dict[str, GovernanceLevel] = {
    level.value: level for level in GovernanceLevel
}


def _parse_reported_level(value: str) -> GovernanceLevel:
    """Return the governance level a heartbeat's raw claim maps to.

    An unrecognised token maps to ``ADMISSION``, the fail-closed reading --
    the same rule :mod:`zeroth.governance.attestations.provider` applies to an
    unrecognised ``claimed_level``.

    Args:
        value: The raw ``reported_level`` string stored on the heartbeat row.

    Returns:
        The matching :class:`GovernanceLevel`, or ``ADMISSION`` for anything
        else.
    """
    return _GOVERNANCE_LEVEL_BY_VALUE.get(value, GovernanceLevel.ADMISSION)


class Heartbeat(BaseModel):
    """One liveness ping from a deployment's adapter, mirrored 1:1 onto a row.

    ``reported_level`` stays a plain ``str`` -- matching how
    ``InventoryRegistration.coverage`` is kept plain in
    :mod:`zeroth.governance.attestations.store` -- because an unexpected token
    from a stored row is a verification concern for the reading resolver, not
    a construction error that should make the row unloadable.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    heartbeat_id: str = Field(default_factory=lambda: uuid4().hex)
    tenant_id: str
    deployment_ref: str
    graph_version: str | None = None
    adapter_version: str | None = None
    observed_at: datetime = Field(default_factory=utc_now)
    reported_level: str
    created_at: datetime = Field(default_factory=utc_now)


def _row_to_heartbeat(row: dict[str, Any]) -> Heartbeat:
    """Rebuild a heartbeat from one ``enforcement_heartbeats`` row.

    Raises:
        Exception: Propagated to the caller on any malformed column (for
            example an unparseable timestamp), so the repository can turn it
            into a fail-closed ``None`` instead of a stored row that half
            loads.
    """
    return Heartbeat(
        heartbeat_id=str(row["heartbeat_id"]),
        tenant_id=str(row["tenant_id"]),
        deployment_ref=str(row["deployment_ref"]),
        graph_version=None if row["graph_version"] is None else str(row["graph_version"]),
        adapter_version=(None if row["adapter_version"] is None else str(row["adapter_version"])),
        observed_at=datetime.fromisoformat(str(row["observed_at"])),
        reported_level=str(row["reported_level"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
    )


@persistence_surface(
    "service.enforcement_heartbeats", probe=named_isolation_probe("_drive_heartbeats")
)
class HeartbeatRepository:
    """Append and read deployment heartbeats, scoped per tenant."""

    def __init__(self, database: AsyncDatabase) -> None:
        self._database = database

    @persistence_operation(ResourceOperation.CREATE)
    async def record(self, heartbeat: Heartbeat) -> Heartbeat:
        """Append one heartbeat row.

        Args:
            heartbeat: The liveness ping to persist. ``heartbeat_id``,
                ``observed_at`` and ``created_at`` default to a fresh id and
                the current time when the caller does not set them.

        Returns:
            The heartbeat exactly as stored.
        """
        async with self._database.transaction(write_lock=True) as connection:
            await connection.execute(
                f"""
                INSERT INTO enforcement_heartbeats ({_HEARTBEAT_COLUMNS})
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    heartbeat.heartbeat_id,
                    heartbeat.tenant_id,
                    heartbeat.deployment_ref,
                    heartbeat.graph_version,
                    heartbeat.adapter_version,
                    heartbeat.observed_at.isoformat(),
                    heartbeat.reported_level,
                    heartbeat.created_at.isoformat(),
                ),
            )
        return heartbeat

    @persistence_operation(ResourceOperation.READ)
    async def latest_for_deployment(
        self,
        tenant_id: str,
        deployment_ref: str,
    ) -> Heartbeat | None:
        """Load the newest heartbeat for one deployment.

        Args:
            tenant_id: The tenant the deployment belongs to.
            deployment_ref: The deployment whose liveness is wanted.

        Returns:
            The newest heartbeat, or ``None`` when this tenant has never
            recorded one for that deployment, or when the stored row could
            not be parsed back into a :class:`Heartbeat`. A malformed row is
            fail-closed absence, never a raised exception.
        """
        async with self._database.transaction() as connection:
            row = await connection.fetch_one(
                _SELECT_LATEST_HEARTBEAT,
                (tenant_id, deployment_ref),
            )
        if row is None:
            return None
        try:
            return _row_to_heartbeat(row)
        except Exception:  # noqa: BLE001 - malformed row reads as absent
            return None


class DeploymentStatusResolver:
    """Resolve a deployment's last-known governance level from heartbeats."""

    def __init__(
        self,
        *,
        heartbeats: HeartbeatRepository,
        tenant_id: str,
        deployment_ref: str,
        stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        """Bind the resolver to one tenant and deployment.

        Args:
            heartbeats: Store of recorded liveness pings.
            tenant_id: Tenant every lookup is scoped to.
            deployment_ref: Deployment whose last-known status is wanted.
            stale_after_seconds: Freshness window handed to
                ``CapabilityReporter``; defaults to
                :data:`DEFAULT_STALE_AFTER_SECONDS`.
            now: Clock used only for documentation parity with the reporter --
                the actual staleness comparison happens inside
                ``CapabilityReporter._validated_level``, against the evidence
                this resolver returns.
        """
        self._heartbeats = heartbeats
        self._tenant_id = tenant_id
        self._deployment_ref = deployment_ref
        self._stale_after_seconds = stale_after_seconds
        self._now = now

    @property
    def stale_after_seconds(self) -> float:
        """Return the freshness window this resolver was bound with."""
        return self._stale_after_seconds

    async def last_known_evidence(self) -> RunCapabilityEvidence | None:
        """Return evidence built from the newest heartbeat, or ``None``.

        ``tool_manifest_complete`` is always ``False``: a heartbeat proves no
        inventory, so it can never satisfy
        ``CapabilityReporter._validated_level``'s ``ENFORCED`` predicate no
        matter what ``reported_level`` claims. ``signature_valid`` is always
        ``True`` -- unlike a run attestation, a heartbeat's trust boundary is
        the tenant-scoped read itself, not a cryptographic signature over its
        payload; the ceiling that stops an over-claiming heartbeat from
        reaching ``ENFORCED`` is ``tool_manifest_complete``, not this flag.

        Returns:
            Evidence derived from the newest stored heartbeat, or ``None``
            when no heartbeat exists for this tenant and deployment, or when
            the stored row could not be read. Never raises.
        """
        try:
            heartbeat = await self._heartbeats.latest_for_deployment(
                self._tenant_id,
                self._deployment_ref,
            )
        except Exception:  # noqa: BLE001 - unreadable state is no evidence
            return None
        if heartbeat is None:
            return None
        try:
            # ``signature_valid=True`` on unsigned data needs its reasoning here, not
            # in a design doc, because that is the field a reader will challenge.
            #
            # A heartbeat carries no signature. On the *run* path this field means
            # "this attestation's signature verified"; on the *deployment* path it is
            # structural -- it is the only gate that lets fresh-vs-stale be observable
            # at all. Setting it False would make ``level_for_deployment`` return
            # ADMISSION unconditionally, which does not make the staleness rule safer,
            # it makes it unfalsifiable. So it records a narrower claim: the
            # tenant-scoped database read is the trust boundary for *last-known
            # deployment* status, which is a weaker claim than per-run enforcement.
            #
            # The real ceiling is ``tool_manifest_complete=False``. That is what blocks
            # ``_validated_level``'s ENFORCED branch, which requires
            # ``governance_level is ENFORCED and tool_manifest_complete``
            # (capabilities.py:86-90), so no heartbeat can ever claim enforcement.
            #
            # This is safe ONLY because ``level_for_run`` discards deployment evidence
            # outright (``del deployment_evidence``, capabilities.py:103), so heartbeat
            # evidence cannot reach the run path. That invariant is pinned by
            # ``test_heartbeat_evidence_does_not_raise_run_with_no_attestation``; if a
            # future change makes ``level_for_run`` fall back to deployment evidence,
            # that test fails rather than this comment silently going stale.
            return RunCapabilityEvidence(
                correlation_id=heartbeat.heartbeat_id,
                governance_level=_parse_reported_level(heartbeat.reported_level),
                observed_at=heartbeat.observed_at,
                graph_version=heartbeat.graph_version,
                signature_valid=True,
                tool_manifest_complete=False,
            )
        except Exception:  # noqa: BLE001 - malformed heartbeat is no evidence
            return None


__all__ = [
    "DEFAULT_STALE_AFTER_SECONDS",
    "DeploymentStatusResolver",
    "Heartbeat",
    "HeartbeatRepository",
]
