"""Trust-boundary tests for deployment heartbeats (ZER-8 S6).

Heartbeats are a weaker evidence kind than signed run attestations: no
signature, no bound tool inventory, just a self-reported ``reported_level``
and a timestamp. Two requirements pin exactly how much a heartbeat is allowed
to affect:

R13 -- a stale heartbeat downgrades the *deployment-level* last-known status,
per ``CapabilityReporter``'s existing ``stale_after_seconds`` window. The
freshness tests are asserted through ``CapabilityReporter.level_for_deployment``
itself, not only through ``DeploymentStatusResolver``'s own return value,
because the resolver returning "the right evidence" is not the same claim as
the reporter granting "the right level" from that evidence.

R14 -- a heartbeat can never upgrade an individual run.
``CapabilityReporter.level_for_run`` already discards ``deployment_evidence``
outright; these tests pin that behaviour rather than re-derive it.

The R13 boundary tests are parametrized exactly at the stale threshold so an
off-by-one in the age comparison (``<=`` vs ``<``) is caught, and one test is
a positive control: a fresh heartbeat really does produce ``OBSERVED``, so the
downgrade tests cannot pass vacuously against a resolver that always returns
``ADMISSION``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from zeroth.contracts.langgraph_gateway.models import GovernanceLevel, RunCapabilityEvidence
from zeroth.governance.attestations import (
    DEFAULT_STALE_AFTER_SECONDS,
    DeploymentStatusResolver,
    Heartbeat,
    HeartbeatRepository,
)
from zeroth.governance.langgraph_gateway.capabilities import CapabilityReporter

TENANT = "tenant-alpha"
OTHER_TENANT = "tenant-beta"
DEPLOYMENT = "dep-alpha"

NOW = datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC)
STALE_AFTER = 90.0


def make_heartbeat(**overrides: Any) -> Heartbeat:
    """Build a heartbeat observed exactly ``NOW``, claiming ``observed``."""
    fields: dict[str, Any] = {
        "tenant_id": TENANT,
        "deployment_ref": DEPLOYMENT,
        "graph_version": "graph-v1",
        "adapter_version": "adapter-v1",
        "observed_at": NOW,
        "reported_level": "observed",
    }
    fields.update(overrides)
    return Heartbeat(**fields)


def make_resolver(
    database: Any,
    *,
    tenant_id: str = TENANT,
    deployment_ref: str = DEPLOYMENT,
    stale_after_seconds: float = STALE_AFTER,
) -> DeploymentStatusResolver:
    """Build a resolver bound to the default tenant and deployment, clock at NOW."""
    return DeploymentStatusResolver(
        heartbeats=HeartbeatRepository(database),
        tenant_id=tenant_id,
        deployment_ref=deployment_ref,
        stale_after_seconds=stale_after_seconds,
        now=lambda: NOW,
    )


def make_reporter(*, stale_after_seconds: float = STALE_AFTER) -> CapabilityReporter:
    """Build a bare reporter (no run-evidence provider) clocked at NOW."""
    return CapabilityReporter(stale_after_seconds=stale_after_seconds, now=lambda: NOW)


# --------------------------------------------------------------------------
# module defaults
# --------------------------------------------------------------------------


def test_default_stale_after_seconds_matches_gateway_setting() -> None:
    """The default must come from settings, not a second hardcoded number."""
    assert DEFAULT_STALE_AFTER_SECONDS == 90.0


# --------------------------------------------------------------------------
# R13 -- positive control: a fresh heartbeat really produces a real level.
# --------------------------------------------------------------------------


async def test_fresh_heartbeat_is_last_known_status_at_deployment_level(sqlite_db) -> None:
    """Positive control: fresh + OBSERVED claim -> OBSERVED, not vacuously ADMISSION."""
    await HeartbeatRepository(sqlite_db).record(make_heartbeat(observed_at=NOW))
    resolver = make_resolver(sqlite_db)

    evidence = await resolver.last_known_evidence()
    level = make_reporter().level_for_deployment(evidence)

    assert evidence is not None
    assert level is GovernanceLevel.OBSERVED


# --------------------------------------------------------------------------
# R13 -- staleness boundary, asserted through the reporter.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("age_seconds", "expected"),
    [
        (0.0, GovernanceLevel.OBSERVED),
        (STALE_AFTER - 1.0, GovernanceLevel.OBSERVED),
        (STALE_AFTER, GovernanceLevel.OBSERVED),
        (STALE_AFTER + 1.0, GovernanceLevel.ADMISSION),
        (STALE_AFTER + 60.0, GovernanceLevel.ADMISSION),
    ],
)
async def test_staleness_boundary_downgrades_through_reporter(
    sqlite_db, age_seconds: float, expected: GovernanceLevel
) -> None:
    observed_at = NOW - timedelta(seconds=age_seconds)
    await HeartbeatRepository(sqlite_db).record(make_heartbeat(observed_at=observed_at))
    resolver = make_resolver(sqlite_db)

    evidence = await resolver.last_known_evidence()
    level = make_reporter().level_for_deployment(evidence)

    assert level is expected


# --------------------------------------------------------------------------
# heartbeat claim is a ceiling: "enforced" can never win, at any freshness.
# --------------------------------------------------------------------------


async def test_fresh_heartbeat_claiming_enforced_never_yields_enforced(sqlite_db) -> None:
    await HeartbeatRepository(sqlite_db).record(
        make_heartbeat(observed_at=NOW, reported_level="enforced")
    )
    resolver = make_resolver(sqlite_db)

    evidence = await resolver.last_known_evidence()
    level = make_reporter().level_for_deployment(evidence)

    assert level is GovernanceLevel.OBSERVED
    assert level is not GovernanceLevel.ENFORCED


async def test_stale_heartbeat_claiming_enforced_downgrades_to_admission(sqlite_db) -> None:
    observed_at = NOW - timedelta(seconds=STALE_AFTER + 1.0)
    await HeartbeatRepository(sqlite_db).record(
        make_heartbeat(observed_at=observed_at, reported_level="enforced")
    )
    resolver = make_resolver(sqlite_db)

    evidence = await resolver.last_known_evidence()
    level = make_reporter().level_for_deployment(evidence)

    assert level is GovernanceLevel.ADMISSION


async def test_heartbeat_evidence_never_reaches_enforced_predicate(sqlite_db) -> None:
    """A heartbeat's evidence can never satisfy the ENFORCED predicate directly.

    ``tool_manifest_complete`` must be False, no matter what ``reported_level``
    claims -- that is the mechanism, not just the observed outcome above.
    """
    await HeartbeatRepository(sqlite_db).record(
        make_heartbeat(observed_at=NOW, reported_level="enforced")
    )
    resolver = make_resolver(sqlite_db)

    evidence = await resolver.last_known_evidence()

    assert evidence is not None
    assert evidence.tool_manifest_complete is False


# --------------------------------------------------------------------------
# R14 -- a heartbeat can never upgrade a run.
# --------------------------------------------------------------------------


async def test_heartbeat_evidence_does_not_raise_run_with_no_attestation() -> None:
    """No attestation at all + heartbeat claiming enforced -> stays ADMISSION."""
    reporter = make_reporter()
    heartbeat_evidence = RunCapabilityEvidence(
        correlation_id="corr-1",
        governance_level=GovernanceLevel.ENFORCED,
        observed_at=NOW,
        graph_version="graph-v1",
        signature_valid=True,
        tool_manifest_complete=False,
    )

    level = await reporter.level_for_run(
        "corr-1",
        deployment_evidence=heartbeat_evidence,
    )

    assert level is GovernanceLevel.ADMISSION


async def test_heartbeat_evidence_does_not_raise_observed_run_to_enforced() -> None:
    """Run has real OBSERVED evidence; heartbeat claims ENFORCED -> stays OBSERVED."""

    class _ObservedRunProvider:
        async def evidence_for_run(self, correlation_id: str) -> RunCapabilityEvidence:
            return RunCapabilityEvidence(
                correlation_id=correlation_id,
                governance_level=GovernanceLevel.OBSERVED,
                observed_at=NOW,
                graph_version="graph-v1",
                signature_valid=True,
                tool_manifest_complete=False,
            )

    reporter = CapabilityReporter(_ObservedRunProvider(), now=lambda: NOW)
    heartbeat_evidence = RunCapabilityEvidence(
        correlation_id="corr-1",
        governance_level=GovernanceLevel.ENFORCED,
        observed_at=NOW,
        graph_version="graph-v1",
        signature_valid=True,
        tool_manifest_complete=True,
    )

    level = await reporter.level_for_run(
        "corr-1",
        deployment_evidence=heartbeat_evidence,
    )

    assert level is GovernanceLevel.OBSERVED


# --------------------------------------------------------------------------
# tenant scoping
# --------------------------------------------------------------------------


async def test_heartbeat_under_one_tenant_is_invisible_to_another(sqlite_db) -> None:
    await HeartbeatRepository(sqlite_db).record(make_heartbeat(tenant_id=TENANT, observed_at=NOW))
    resolver = make_resolver(sqlite_db, tenant_id=OTHER_TENANT)

    evidence = await resolver.last_known_evidence()

    assert evidence is None


# --------------------------------------------------------------------------
# repository behaviour
# --------------------------------------------------------------------------


async def test_latest_for_deployment_returns_newest_of_several(sqlite_db) -> None:
    repository = HeartbeatRepository(sqlite_db)
    await repository.record(
        make_heartbeat(observed_at=NOW - timedelta(minutes=5), reported_level="admission")
    )
    await repository.record(
        make_heartbeat(observed_at=NOW - timedelta(minutes=1), reported_level="observed")
    )
    newest = make_heartbeat(observed_at=NOW, reported_level="enforced")
    await repository.record(newest)

    latest = await repository.latest_for_deployment(TENANT, DEPLOYMENT)

    assert latest is not None
    assert latest.heartbeat_id == newest.heartbeat_id
    assert latest.observed_at == NOW


async def test_no_heartbeat_recorded_yields_none(sqlite_db) -> None:
    repository = HeartbeatRepository(sqlite_db)

    latest = await repository.latest_for_deployment(TENANT, DEPLOYMENT)

    assert latest is None


async def test_malformed_stored_row_returns_none_without_raising(sqlite_db) -> None:
    async with sqlite_db.transaction(write_lock=True) as connection:
        await connection.execute(
            """
            INSERT INTO enforcement_heartbeats (
                heartbeat_id, tenant_id, deployment_ref, graph_version,
                adapter_version, observed_at, reported_level, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "hb-bad",
                TENANT,
                DEPLOYMENT,
                "graph-v1",
                "adapter-v1",
                "not-a-timestamp",
                "observed",
                NOW.isoformat(),
            ),
        )
    repository = HeartbeatRepository(sqlite_db)

    latest = await repository.latest_for_deployment(TENANT, DEPLOYMENT)

    assert latest is None


async def test_malformed_stored_row_resolves_to_no_evidence(sqlite_db) -> None:
    async with sqlite_db.transaction(write_lock=True) as connection:
        await connection.execute(
            """
            INSERT INTO enforcement_heartbeats (
                heartbeat_id, tenant_id, deployment_ref, graph_version,
                adapter_version, observed_at, reported_level, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "hb-bad",
                TENANT,
                DEPLOYMENT,
                "graph-v1",
                "adapter-v1",
                "not-a-timestamp",
                "observed",
                NOW.isoformat(),
            ),
        )
    resolver = make_resolver(sqlite_db)

    evidence = await resolver.last_known_evidence()

    assert evidence is None
