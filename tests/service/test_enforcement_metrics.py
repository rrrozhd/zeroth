"""Observability of the enforcement decision and attestation paths (ZER-8 S9).

A governed tool call that is denied, and a run attestation that lost to an
earlier one, are both *ordinary* outcomes on the wire -- a 200 and a 409. Left
uninstrumented, a deployment whose decisions have silently gone all-deny, or
whose adapters are all losing their attestation race, looks exactly like a
healthy one from the outside.

These tests pin two facts that a component-level metrics test cannot:

* the counters land on the same ``MetricsCollector`` the ``/metrics`` endpoint
  renders, so they are actually scrapeable rather than accumulating on a
  private registry nobody reads;
* the label distinguishes outcomes that matter operationally -- a verdict from
  a conflict from an outage, a recorded attestation from a superseded one.

Cardinality is deliberately bounded: the labels are closed vocabularies
(verdict kind, attestation outcome). Tenant and deployment are never labels --
they are unbounded, and a metrics registry is not an audit trail.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient

from tests.service.helpers import api_key_headers, scoped_auth_config
from tests.service.test_enforcement_api import (
    SECRET_A,
    TENANT_A,
    _decision_body,
    _single_tenant_app,
)
from zeroth.governance.identity import ServiceRole

ADMIN_SECRET = "enforcement-metrics-admin"

ATTESTATION_COUNTER = "zeroth_enforcement_attestations_total"
DECISION_COUNTER = "zeroth_enforcement_decisions_total"


def _counters(app: Any) -> dict[str, float]:
    """Read the counters off the collector the application actually holds."""
    return app.state.bootstrap.metrics_collector.snapshot()["counters"]


def _totalled(counters: dict[str, float], name: str) -> float:
    """Sum every labelled series of one counter."""
    return sum(value for key, value in counters.items() if key.startswith(name))


def _attestation_body(
    deployment_ref: str,
    *,
    correlation_id: str,
    fingerprint: str = "sha256:inventory",
) -> dict:
    return {
        "correlation_id": correlation_id,
        "deployment_ref": deployment_ref,
        "graph_version": "graph-v1",
        "adapter_version": "1",
        "inventory_fingerprint": fingerprint,
        # ``inventory_coverage`` and ``tool_count`` are no longer part of this
        # body: the server derives both from the stored registration.
        "claimed_level": "enforced",
    }


async def test_a_decision_increments_a_scrapeable_verdict_counter(sqlite_db) -> None:
    """R18: the decision path reports, and reports onto the scraped registry.

    The scrape uses an admin credential because ``METRICS_READ`` is admin-only
    while ``ENFORCEMENT_REPORT`` is an operator permission -- the two halves of
    this assertion are genuinely made by different principals, as in production.
    """
    auth_config = scoped_auth_config(
        ("operator-a", SECRET_A, ServiceRole.OPERATOR, TENANT_A, None),
        ("admin-a", ADMIN_SECRET, ServiceRole.ADMIN, TENANT_A, None),
    )
    app, deployment = await _single_tenant_app(sqlite_db, auth_config=auth_config)

    with TestClient(app) as client:
        response = client.post(
            "/v1/enforcement/decisions",
            json=_decision_body(deployment.deployment_ref, key="metrics-allow"),
            headers=api_key_headers(SECRET_A),
        )
        assert response.status_code == 200
        scraped = client.get("/v1/metrics", headers=api_key_headers(ADMIN_SECRET))
        assert scraped.status_code == 200

    assert _totalled(_counters(app), DECISION_COUNTER) == 1.0
    # Scrapeable, not merely recorded: a counter on a private collector would
    # satisfy the assertion above and still be invisible in production.
    assert DECISION_COUNTER in scraped.text


async def test_an_idempotency_conflict_is_counted_apart_from_a_verdict(sqlite_db) -> None:
    """R18: a 409 is a distinct outcome, not folded into the verdict it isn't."""
    app, deployment = await _single_tenant_app(sqlite_db)

    with TestClient(app) as client:
        headers = api_key_headers(SECRET_A)
        first = client.post(
            "/v1/enforcement/decisions",
            json=_decision_body(deployment.deployment_ref, key="metrics-conflict"),
            headers=headers,
        )
        conflict = client.post(
            "/v1/enforcement/decisions",
            json=_decision_body(
                deployment.deployment_ref,
                key="metrics-conflict",
                arguments_digest="sha256:different",
            ),
            headers=headers,
        )

    assert first.status_code == 200
    assert conflict.status_code == 409

    counters = _counters(app)
    conflicts = [key for key in counters if key.startswith(DECISION_COUNTER) and "conflict" in key]
    assert len(conflicts) == 1
    assert counters[conflicts[0]] == 1.0


async def test_a_superseded_attestation_is_counted_apart_from_a_recorded_one(
    sqlite_db,
) -> None:
    """R18: the attestation path distinguishes what it stored from what it refused.

    Both submissions are well-formed and authenticated. Only the label tells an
    operator that the second run's claims are not what the run is judged on.

    The second submission changes a claim -- it declares a different inventory
    fingerprint under the same correlation. It used to be a *byte-identical*
    body, which made this test assert the defect audit round 4 found: an
    honest retry was answered 409 because the server had stamped it with a
    fresh ``issued_at``. An identical body is now the retry case, pinned by
    ``test_an_identical_attestation_retry_is_counted_as_such`` below; a
    superseded attestation is one whose claims genuinely differ, which is what
    this test is about.
    """
    app, deployment = await _single_tenant_app(sqlite_db)

    with TestClient(app) as client:
        headers = api_key_headers(SECRET_A)
        first = client.post(
            "/v1/enforcement/attestations",
            json=_attestation_body(deployment.deployment_ref, correlation_id="metrics-corr"),
            headers=headers,
        )
        second = client.post(
            "/v1/enforcement/attestations",
            json=_attestation_body(
                deployment.deployment_ref,
                correlation_id="metrics-corr",
                fingerprint="sha256:superseding",
            ),
            headers=headers,
        )

    assert first.status_code == 201
    assert second.status_code == 409

    counters = _counters(app)
    assert _totalled(counters, ATTESTATION_COUNTER) == 2.0
    # Two submissions, two distinct labelled series -- one recorded, one not.
    series = [key for key in counters if key.startswith(ATTESTATION_COUNTER)]
    assert len(series) == 2


async def test_an_identical_attestation_retry_is_counted_as_such(sqlite_db) -> None:
    """R18: a retry is authoritative on the wire and still visible to an operator.

    The response to a retry is deliberately indistinguishable from the original
    acceptance -- that is what idempotency means -- so the wire cannot be where
    an operator learns that adapters are retrying. The counter is: a retry
    lands on ``already_recorded`` rather than inflating ``recorded``, which
    keeps "attestations this deployment stored" an honest number.
    """
    app, deployment = await _single_tenant_app(sqlite_db)

    with TestClient(app) as client:
        headers = api_key_headers(SECRET_A)
        body = _attestation_body(deployment.deployment_ref, correlation_id="metrics-retry")
        first = client.post("/v1/enforcement/attestations", json=body, headers=headers)
        second = client.post("/v1/enforcement/attestations", json=body, headers=headers)

    assert first.status_code == 201
    assert second.status_code == 201

    counters = _counters(app)
    assert _totalled(counters, ATTESTATION_COUNTER) == 2.0
    recorded = [key for key in counters if key.startswith(ATTESTATION_COUNTER)]
    assert len(recorded) == 2
    assert [key for key in recorded if "already_recorded" in key]


async def test_a_same_clock_duplicate_is_not_counted_as_a_second_recording(
    sqlite_db,
    monkeypatch,
) -> None:
    """R18: an envelope-identical duplicate does not inflate ``recorded``.

    The route stamps ``issued_at`` from its own clock, so two submissions
    normally differ in the signed bytes. Two *concurrent* ones need not: land
    them in the same microsecond and they build the same payload, which a
    deterministic signer turns into the same envelope. ``record`` then reports
    the loser as authoritative -- correctly, its envelope *is* the one in force
    -- and the audited defect was what happened next: that loser never reached
    the claims comparison and was counted ``recorded``, so ``recorded``
    overstated how many attestations the deployment had actually stored.

    The clock is frozen rather than the requests raced, because a race would
    make the test pass for timing reasons rather than for the reason under test.
    Freezing reproduces exactly the state a collision produces.
    """
    app, deployment = await _single_tenant_app(sqlite_db)
    frozen = datetime(2026, 7, 31, 12, 0, 0, 123456, tzinfo=UTC)
    monkeypatch.setattr(
        "zeroth.service.api.enforcement_api.utc_now",
        lambda: frozen,
    )

    with TestClient(app) as client:
        headers = api_key_headers(SECRET_A)
        body = _attestation_body(deployment.deployment_ref, correlation_id="metrics-collide")
        first = client.post("/v1/enforcement/attestations", json=body, headers=headers)
        second = client.post("/v1/enforcement/attestations", json=body, headers=headers)

    # Both are authoritative, and identically so: the envelopes are equal.
    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json() == first.json()

    counters = _counters(app)
    assert _totalled(counters, ATTESTATION_COUNTER) == 2.0
    # One insert, one duplicate -- not two recordings. Asserted on the exact
    # labelled series, since a substring match on "recorded" also matches
    # "already_recorded".
    assert counters[f'{ATTESTATION_COUNTER}{{outcome="recorded"}}'] == 1.0
    assert counters[f'{ATTESTATION_COUNTER}{{outcome="already_recorded"}}'] == 1.0
