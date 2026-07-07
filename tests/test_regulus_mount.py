"""In-process mount of the bundled Regulus control plane (src/econ_plane).

When ``regulus`` is enabled (a non-None ``bootstrap.regulus_client``),
``create_app`` mounts econ_plane under ``/regulus`` and drives its bootstrap from
Zeroth's lifespan. The mount sits **behind Zeroth's API-key gate** (no bypass);
econ_plane then enforces its own JWT. Zeroth's own self-calls carry both
credentials via the ``regulus_self_auth_headers`` provider.

These tests assert the two things the design hinges on:

* **Gate (security):** the open econ ``/auth/token`` issuer is unreachable
  without Zeroth's ``X-API-Key`` once mounted.
* **Self-auth + persistence:** the real self-auth provider authenticates through
  the gate *and* econ_plane, and an ingested execution actually persists.

Plus the fail-open boundary (D-12): a bad econ token -> budget still allows.

Skips cleanly when the ``regulus`` extra (econ_plane + python-jose) is absent.
"""

from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime

import pytest

# econ_plane binds its SQLAlchemy engine to ECP_DATABASE_URL at import time, so
# set it to a throwaway SQLite file BEFORE econ_plane is first imported.
_DB_FD, _DB_PATH = tempfile.mkstemp(suffix="_econ_mount.db")
os.close(_DB_FD)
os.environ["ECP_DATABASE_URL"] = f"sqlite+pysqlite:///{_DB_PATH}"

pytest.importorskip("econ_plane", reason="requires the 'regulus' extra")

from fastapi.testclient import TestClient  # noqa: E402

from zeroth.core.econ.budget import BudgetEnforcer  # noqa: E402
from zeroth.core.econ.service_auth import (  # noqa: E402
    make_self_auth_headers_provider,
    mint_econ_service_token,
)
from zeroth.core.identity import ServiceRole  # noqa: E402
from zeroth.core.service.app import create_app  # noqa: E402
from zeroth.core.service.auth import (  # noqa: E402
    ServiceAuthConfig,
    ServiceAuthenticator,
    StaticApiKeyCredential,
)

_ZEROTH_KEY = "test-zeroth-service-key"
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

    def stop(self) -> None:
        pass


class _GatedBootstrap:
    """regulus enabled (non-None regulus_client) + a real authenticator so the
    gated /regulus subtree behaves like production."""

    regulus_client = _StubRegulusClient()
    auth_config = _AUTH_CONFIG
    authenticator = ServiceAuthenticator(_AUTH_CONFIG)


class _DisabledBootstrap:
    regulus_client = None
    auth_config = _AUTH_CONFIG
    authenticator = ServiceAuthenticator(_AUTH_CONFIG)


def test_mount_absent_when_regulus_disabled() -> None:
    app = create_app(_DisabledBootstrap())
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/regulus" not in paths


def test_open_token_issuer_is_unreachable_over_http() -> None:
    """Decision 1: mounting must not expose econ's open Admin-token issuer.

    econ_plane mints an Admin JWT for any caller of POST /**/auth/token with no
    credential check. Zeroth blocks that endpoint at its gate (404) so no external
    caller -- authenticated or not -- can escalate to econ Admin. Zeroth's own
    self-calls mint the token in-process, never via this endpoint.
    """
    app = create_app(_GatedBootstrap())
    assert "/regulus" in {getattr(r, "path", "") for r in app.routes}

    body = {"sub": "x", "email": "x@x.io", "roles": ["Admin"]}
    with TestClient(app) as client:
        # No Zeroth API key -> 404 (blocked before the gate even authenticates).
        assert client.post("/regulus/v1/auth/token", json=body).status_code == 404
        # Even WITH a valid Zeroth API key -> still 404: the issuer is not on the
        # HTTP surface, so an authenticated principal cannot mint an econ Admin token.
        blocked = client.post(
            "/regulus/v1/auth/token", json=body, headers={"X-API-Key": _ZEROTH_KEY}
        )
        assert blocked.status_code == 404
        # Trailing-slash variant is blocked too.
        assert (
            client.post(
                "/regulus/v1/auth/token/", json=body, headers={"X-API-Key": _ZEROTH_KEY}
            ).status_code
            == 404
        )


def test_self_auth_provider_persists_execution_through_gate_and_mount() -> None:
    """Decision 2: the real self-auth provider authenticates through BOTH the
    Zeroth gate and econ_plane, and the ingested execution persists."""
    app = create_app(_GatedBootstrap())

    # The provider create_app published is what cost_api uses; it carries
    # X-API-Key (Zeroth gate) + a fresh econ Admin JWT.
    provider = app.state.regulus_self_auth_headers
    headers = provider()
    assert headers["X-API-Key"] == _ZEROTH_KEY
    assert headers["Authorization"].startswith("Bearer ")

    exec_id = "exec_selfauth_persist"
    with TestClient(app) as client:
        # Seed capability/implementation (same self-auth headers).
        client.post(
            "/regulus/v1/capabilities",
            headers=headers,
            json={
                "id": "FraudDetection",
                "name": "Fraud",
                "category": "RiskMitigation",
                "valuation_config": {"proxy": "fraud_delta_x_loss"},
            },
        )
        client.post(
            "/regulus/v1/capabilities/FraudDetection/implementations",
            headers=headers,
            json={"id": "model_v1", "name": "Model v1", "model_version": "1.0"},
        )
        ingest = client.post(
            "/regulus/v1/instrumentation/executions",
            headers=headers,
            json={
                "execution_id": exec_id,
                "timestamp": datetime.now(UTC).isoformat(),
                "capability_id": "FraudDetection",
                "implementation_id": "model_v1",
                "model_version": "1.0",
                "token_cost_usd": "0.0123",
                "tool_cost_usd": "0.0",
                "compute_cost_usd": "0.0",
                "latency_ms": 42,
                "compute_time_ms": 10,
                "metadata": {"tenant_id": "tenant_default"},
            },
        )
        assert ingest.status_code == 200, ingest.text
        assert ingest.json()["status"] == "inserted"

    # Persistence proof: the row is actually in econ_plane's database.
    from econ_plane.database import SessionLocal
    from econ_plane.instrumentation.models import ExecutionEvent

    with SessionLocal() as db:
        row = db.query(ExecutionEvent).filter_by(execution_id=exec_id).one_or_none()
    assert row is not None


def test_minted_service_token_is_admin_and_decodable() -> None:
    """The minted econ token is valid and carries the Admin role."""
    from econ_plane.auth.service import decode_token

    token = mint_econ_service_token()
    assert token is not None
    claims = decode_token(token)
    assert "Admin" in claims.roles


@pytest.mark.asyncio
async def test_budget_fails_open_on_bad_token_but_sends_headers() -> None:
    """Decision-2 wiring is present (headers attached) AND the D-12 fail-open
    boundary still holds when econ rejects the token (401)."""
    seen: dict[str, str] = {}

    def handler(request):  # httpx.MockTransport handler
        seen.update(request.headers)
        import httpx

        return httpx.Response(401, json={"detail": "Not authenticated"})

    provider = make_self_auth_headers_provider(_ZEROTH_KEY)
    enforcer = BudgetEnforcer(
        "http://regulus.test/v1",
        headers_provider=provider,
        _transport=handler,
    )
    allowed, spend, cap = await enforcer.check_budget("tenant_default")

    # Fail-open: a 401 must not block execution.
    assert allowed is True
    assert spend == 0.0
    assert cap == float("inf")
    # ...but the self-auth headers were attached to the outbound request.
    assert seen.get("x-api-key") == _ZEROTH_KEY
    assert seen.get("authorization", "").startswith("Bearer ")


@pytest.mark.asyncio
async def test_tenant_budget_cap_persists_and_trips_the_enforcer() -> None:
    """End-to-end budget closure: a cap set on the bundled econ plane, plus an
    ingested execution that exceeds it, makes the REAL /budget/status payload
    deny — parsed by the actual BudgetEnforcer, not a hand-mocked contract.

    This is the contract test whose absence let the enforcer ship pointed at
    /dashboard/kpis reading fields that endpoint never returned.
    """
    import httpx

    app = create_app(_GatedBootstrap())
    provider = app.state.regulus_self_auth_headers
    headers = provider()

    with TestClient(app) as client:
        # Set a one-cent cap for tenant "acme" on the mounted econ plane.
        put = client.put(
            "/regulus/v1/budget/tenants/acme",
            headers=headers,
            json={"budget_cap_usd": 0.01},
        )
        assert put.status_code == 200, put.text
        assert put.json()["budget_cap_usd"] == 0.01

        # Ingest spend above the cap, carrying the first-class tenant_id the
        # SDK event now emits.
        ingest = client.post(
            "/regulus/v1/instrumentation/executions",
            headers=headers,
            json={
                "execution_id": "exec_budget_trip",
                "timestamp": datetime.now(UTC).isoformat(),
                "capability_id": "node-agent",
                "implementation_id": "openai/gpt-4o-mini",
                "model_version": "gpt-4o-mini",
                "token_cost_usd": "0.02",
                "tool_cost_usd": "0.0",
                "compute_cost_usd": "0.0",
                "latency_ms": 10,
                "compute_time_ms": 5,
                "tenant_id": "acme",
                "metadata": {"run_id": "r1"},
            },
        )
        assert ingest.status_code == 200, ingest.text

        status = client.get(
            "/regulus/v1/budget/status",
            params={"tenant_id": "acme"},
            headers=headers,
        )
        assert status.status_code == 200, status.text
        real_payload = status.json()
        assert real_payload["total_cost_usd"] >= 0.02
        assert real_payload["budget_cap_usd"] == 0.01

    # Feed the REAL payload through the enforcer and assert it targets the
    # real endpoint path.
    requested: dict[str, str] = {}

    def handler(request):
        requested["path"] = request.url.path
        return httpx.Response(200, json=real_payload)

    enforcer = BudgetEnforcer("http://regulus.test/v1", _transport=handler)
    allowed, spend, cap = await enforcer.check_budget("acme")

    assert requested["path"] == "/v1/budget/status"
    assert allowed is False
    assert spend >= 0.02
    assert cap == 0.01


@pytest.mark.asyncio
async def test_enforcer_treats_missing_cap_as_unlimited() -> None:
    """A tenant with no configured cap (budget_cap_usd null) is unlimited,
    not a fail-open warning."""
    import httpx

    def handler(request):
        return httpx.Response(
            200,
            json={"tenant_id": "capless", "total_cost_usd": 12.5, "budget_cap_usd": None},
        )

    enforcer = BudgetEnforcer("http://regulus.test/v1", _transport=handler)
    allowed, spend, cap = await enforcer.check_budget("capless")

    assert allowed is True
    assert spend == 12.5
    assert cap == float("inf")
