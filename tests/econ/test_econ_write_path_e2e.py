"""End-to-end proof of the econ cost-event WRITE path (audit P0).

Before the fix the bundled deploy built its ``RegulusClient`` pointed at the
EXTERNAL ``localhost:8000`` default and posted every cost event over a plain
``httpx.Client`` — so in a bundled deploy (where the plane is mounted in-process,
nothing listening on :8000) every event connection-refused, retried, and dropped.
Spend therefore stayed ``0.0`` forever and budget caps enforced against ``$0``.

This test drives the REAL write path (``RegulusClient(asgi_app=<mounted plane>)``
-> ``TelemetryTransport`` -> ``httpx.ASGITransport`` -> ingest) in-process and
asserts the observable effect: a tracked cost event makes tenant spend transition
``0.0 -> > 0.0`` and, with a cap seeded below that spend, the enforcer denies.
Run against the buggy transport (asgi_app not wired) the spend assertion fails —
the exact P0 inversion.
"""

from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime
from decimal import Decimal

import pytest

# zeroth.econ.plane binds its SQLAlchemy engine to ECP_DATABASE_URL at import
# time, so point it at a throwaway SQLite file BEFORE the plane is first imported.
_DB_FD, _DB_PATH = tempfile.mkstemp(suffix="_econ_write_path.db")
os.close(_DB_FD)
os.environ["ECP_DATABASE_URL"] = f"sqlite+pysqlite:///{_DB_PATH}"

pytest.importorskip("zeroth.econ.plane", reason="requires the 'regulus' extra")

from fastapi.testclient import TestClient  # noqa: E402

from zeroth.econ.analytics.budget import BudgetEnforcer  # noqa: E402
from zeroth.econ.analytics.client import RegulusClient  # noqa: E402
from zeroth.econ.analytics.service_auth import make_self_auth_headers_provider  # noqa: E402
from zeroth.econ.instrumentation import ExecutionEvent  # noqa: E402
from zeroth.governance.identity import ServiceRole  # noqa: E402
from zeroth.service.api.authentication import (  # noqa: E402
    ServiceAuthConfig,
    ServiceAuthenticator,
    StaticApiKeyCredential,
)
from zeroth.service.app import create_app  # noqa: E402

_ZEROTH_KEY = "test-zeroth-write-path-key"
_AUTH_CONFIG = ServiceAuthConfig(
    api_keys=[
        StaticApiKeyCredential(
            credential_id="svc",
            secret=_ZEROTH_KEY,
            subject="svc",
            roles=[ServiceRole.ADMIN],
            tenant_id="default",
        )
    ]
)


class _StubRegulusClient:
    base_url = "http://localhost:8000/v1"

    def stop(self) -> None:  # pragma: no cover - trivial
        pass


class _GatedBootstrap:
    """Mount the plane (regulus_client non-None) with a real authenticator; the
    stub client is only there to trigger the mount + DB bootstrap in the lifespan.
    The write path under test uses its OWN real RegulusClient below."""

    regulus_client = _StubRegulusClient()
    auth_config = _AUTH_CONFIG
    authenticator = ServiceAuthenticator(_AUTH_CONFIG)


@pytest.mark.asyncio
async def test_write_path_lands_cost_event_and_trips_cap_in_process(monkeypatch) -> None:
    from zeroth.econ.plane.config import settings as ecp_settings
    from zeroth.econ.plane.main import app as econ_plane_app

    # A dedicated tenant: the econ-plane engine binds once per process, so this DB
    # is shared with every later test in a full-suite run. Capping tenant "default"
    # here would budget-deny every subsequent service test that actually runs an
    # agent (the write path works now, so the cap really enforces).
    tenant = "wp-e2e-tenant"
    monkeypatch.setattr(ecp_settings, "service_principal_tenant_id", tenant)

    app = create_app(_GatedBootstrap())

    # The parent lifespan bootstraps econ_plane's DB (creates tables in the temp
    # file). Inside that context, drive the REAL cost-event write path exactly as
    # the factory now wires it: internal base_url + asgi_app=<mounted plane>.
    with TestClient(app):
        regulus_client = RegulusClient(
            base_url="http://regulus.internal/v1",
            enabled=True,
            headers_provider=make_self_auth_headers_provider(_ZEROTH_KEY),
            asgi_app=econ_plane_app,
        )
        # capability_id/implementation_id are platform-shaped (a node id + a model
        # name) that the bundled plane never pre-registers — so this also exercises
        # ingest auto-registration; without it the event would 422 and drop.
        event = ExecutionEvent(
            capability_id="wp-node",
            implementation_id="wp-model",
            model_version="wp-model",
            tenant_id=tenant,
            token_cost_usd=Decimal("0.02"),
            latency_ms=1,
            compute_time_ms=1,
            metadata={"run_id": "wp-r1"},
        )
        regulus_client.track_execution(event)
        # Synchronous in-process flush over ASGITransport into the mounted plane.
        regulus_client._client.transport.flush_once()
        assert regulus_client._client.transport.dropped_events == 0
        regulus_client.stop()

    # Read spend back through the same mounted plane (the READ seam already had the
    # in-process dispatch; this proves the WRITE seam now agrees with it).
    enforcer = BudgetEnforcer(
        asgi_app=econ_plane_app,
        headers_provider=make_self_auth_headers_provider(_ZEROTH_KEY),
    )
    allowed, spend, cap = await enforcer.check_budget(tenant)
    # THE P0 INVERSION: spend was always 0.0 before the fix (event dropped).
    assert spend >= 0.02, f"cost event did not land in-process: spend={spend}"

    # Now seed a cap below spend and prove the cap actually trips.
    with TestClient(app) as client:
        headers = app.state.regulus_self_auth_headers()
        put = client.put(
            f"/regulus/v1/budget/tenants/{tenant}",
            headers=headers,
            json={"budget_cap_usd": 0.01},
        )
        assert put.status_code == 200, put.text

    trip_enforcer = BudgetEnforcer(
        asgi_app=econ_plane_app,
        headers_provider=make_self_auth_headers_provider(_ZEROTH_KEY),
    )
    status = await trip_enforcer.check_budget_status(tenant)
    assert status.allowed is False
    assert status.spend_usd >= 0.02
    assert status.cap_usd == 0.01


@pytest.mark.asyncio
async def test_factory_wires_asgi_app_into_regulus_write_path(sqlite_db) -> None:
    """The REAL bootstrap must hand the mounted plane to the write-path transport,
    not just to the read-path enforcer — otherwise events still drop."""
    from tests.service.helpers import agent_graph, deploy_service

    service, _ = await deploy_service(sqlite_db, agent_graph(graph_id="write-path-wiring"))
    try:
        assert service.regulus_client is not None
        transport = service.regulus_client._client.transport
        assert transport._asgi_app is not None, "write path not wired to the in-process plane"
        # base_url stays the resolvable configured value (the readiness probe HTTP-GETs
        # it); the in-process dispatch is proven by transport._asgi_app above, since
        # ASGITransport routes by path and ignores the host.
        from zeroth.platform.config.settings import get_settings

        assert service.regulus_client.base_url == get_settings().regulus.base_url
    finally:
        if service.regulus_client is not None:
            service.regulus_client.stop()
