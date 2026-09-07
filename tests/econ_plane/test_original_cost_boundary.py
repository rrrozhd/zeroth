"""Original charge assertions must survive storage and exact HTTP retries."""

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.econ_plane.test_charge_cost_revisions import client as http_client
from tests.econ_plane.test_sdk_evidence_namespace import NOW, engine as database_engine
from zeroth.econ.plane.instrumentation.models import ExecutionEvent

engine = database_engine
client = http_client


@pytest.mark.parametrize("amount", ["0.00000001", "9999999999.12345678"])
def test_original_charge_precision_and_exact_retry(engine, client, amount):
    payload = dict(
        event_id="source-charge",
        workflow="invoice",
        workflow_version="v1",
        run_id="source-run",
        step="provider",
        recorded_at=NOW.isoformat(),
        cost_role="charge",
        charge_id="account:request-1",
        cost_usd=amount,
    )
    response = client.post("/v1/executions", json=payload)
    assert response.status_code == 200, response.text
    with Session(engine) as raw:
        cost = raw.scalars(select(ExecutionEvent.token_cost_usd)).one()
        assert cost == Decimal(amount)
    repeated = client.post("/v1/executions", json=payload)
    assert repeated.status_code == 200 and repeated.json()["status"] == "duplicate"


@pytest.mark.parametrize("amount", ["0.000000001", "10000000000"])
def test_original_charge_rejects_unrepresentable_amounts_before_storage(engine, client, amount):
    payload = dict(
        event_id="source-charge",
        workflow="invoice",
        workflow_version="v1",
        run_id="source-run",
        step="provider",
        recorded_at=NOW.isoformat(),
        cost_role="charge",
        charge_id="account:request-1",
        cost_usd=amount,
    )
    # Use a client configuration that observes server errors as HTTP responses.
    client._transport.raise_server_exceptions = False
    response = client.post("/v1/executions", json=payload)
    assert response.status_code == 422, response.text
    with Session(engine) as raw:
        assert list(raw.scalars(select(ExecutionEvent))) == []


def test_original_owned_charge_rejects_fractional_json_numbers(client):
    # Deliberately raw bytes: a Python JSON encoder must not round the probe first.
    raw = """{"event_id":"source-charge","workflow":"invoice","workflow_version":"v1",
      "run_id":"source-run","step":"provider","recorded_at":"2026-09-06T00:00:00Z",
      "cost_role":"charge","charge_id":"account:request-1","cost_usd":9999999999.12345678}"""
    response = client.post(
        "/v1/executions", content=raw, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert "decimal strings" in response.text
