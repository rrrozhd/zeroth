"""Execution-ingest auto-registration (audit P0, finding 2).

Platform-emitted execution telemetry names its capability by node_id and its
implementation by model_name — rows the bundled deploy never pre-registers. With
``ECP_AUTO_REGISTER_INGEST_CAPABILITIES`` on (default), the EXECUTION ingest path
upserts those rows so the event lands (200) instead of 422-ing on the strict
existence guard. The switch is execution-only: with it off the strict guard
returns, and the OUTCOME path is never relaxed (pinned elsewhere by
test_outcome_batch_and_erasure_identity).
"""

from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime

import pytest

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix="_econ_autoregister.db")
os.close(_DB_FD)
os.environ["ECP_DATABASE_URL"] = f"sqlite+pysqlite:///{_DB_PATH}"

pytest.importorskip("zeroth.econ.plane", reason="requires the 'regulus' extra")

from fastapi.testclient import TestClient  # noqa: E402

from zeroth.governance.identity import ServiceRole  # noqa: E402
from zeroth.service.api.authentication import (  # noqa: E402
    ServiceAuthConfig,
    ServiceAuthenticator,
    StaticApiKeyCredential,
)
from zeroth.service.app import create_app  # noqa: E402

_ZEROTH_KEY = "test-autoregister-key"
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
    regulus_client = _StubRegulusClient()
    auth_config = _AUTH_CONFIG
    authenticator = ServiceAuthenticator(_AUTH_CONFIG)


def _execution_payload(execution_id: str, *, capability: str, implementation: str) -> dict:
    return {
        "execution_id": execution_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "capability_id": capability,
        "implementation_id": implementation,
        "model_version": "gpt-4o-mini",
        "token_cost_usd": "0.01",
        "tool_cost_usd": "0.0",
        "compute_cost_usd": "0.0",
        "latency_ms": 10,
        "compute_time_ms": 5,
        "tenant_id": "ar-e2e-tenant",
        "metadata": {"run_id": "r1"},
    }


def test_execution_ingest_autoregisters_unknown_capability(monkeypatch) -> None:
    from zeroth.econ.plane.config import settings as ecp_settings

    monkeypatch.setattr(ecp_settings, "service_principal_tenant_id", "ar-e2e-tenant")
    assert ecp_settings.auto_register_ingest_capabilities is True  # default ON

    app = create_app(_GatedBootstrap())
    with TestClient(app) as client:
        headers = app.state.regulus_self_auth_headers()
        resp = client.post(
            "/regulus/v1/instrumentation/executions",
            headers=headers,
            json=_execution_payload("ar-exec-1", capability="node-x", implementation="gpt-4o"),
        )
        assert resp.status_code == 200, resp.text

        # The same model (implementation id) reused under a DIFFERENT node must not
        # PK-collide or 422 — existence is by implementation id alone.
        resp2 = client.post(
            "/regulus/v1/instrumentation/executions",
            headers=headers,
            json=_execution_payload("ar-exec-2", capability="node-y", implementation="gpt-4o"),
        )
        assert resp2.status_code == 200, resp2.text


def test_execution_ingest_strict_when_autoregister_disabled(monkeypatch) -> None:
    from zeroth.econ.plane.config import settings as ecp_settings

    monkeypatch.setattr(ecp_settings, "service_principal_tenant_id", "ar-e2e-tenant")
    monkeypatch.setattr(ecp_settings, "auto_register_ingest_capabilities", False)

    app = create_app(_GatedBootstrap())
    with TestClient(app) as client:
        headers = app.state.regulus_self_auth_headers()
        resp = client.post(
            "/regulus/v1/instrumentation/executions",
            headers=headers,
            json=_execution_payload(
                "ar-exec-strict", capability="never-registered", implementation="never-model"
            ),
        )
        assert resp.status_code == 422, resp.text
        assert "capability does not exist" in resp.text
