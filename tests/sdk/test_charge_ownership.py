"""Explicit monetary ownership survives lean SDK and runtime HTTP contracts."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from zeroth.econ.instrumentation.schemas import ExecutionEvent as RuntimeExecution
from zeroth.econ.plane.cloud.schemas import SdkExecutionEvent
from zeroth.econ.plane.instrumentation.schemas import ExecutionEventCreate
from zeroth.protocol import ExecutionEvent as SdkExecution


def event_payload(model, **changes):
    payload = {"cost_role": "charge", "charge_id": "account:request", **changes}
    if model in (SdkExecution, SdkExecutionEvent):
        return {
            "workflow": "invoice",
            "run_id": "run",
            "step": "step",
            "recorded_at": datetime(2026, 9, 6, tzinfo=UTC),
            **payload,
        }
    return {
        "capability_id": "invoice",
        "implementation_id": "v1",
        "model_version": "model",
        "execution_id": "event",
        "timestamp": datetime(2026, 9, 6, tzinfo=UTC),
        **payload,
    }


@pytest.mark.parametrize(
    "model", [SdkExecution, SdkExecutionEvent, RuntimeExecution, ExecutionEventCreate]
)
def test_every_execution_contract_roundtrips_owned_and_unknown_money(model):
    event = model.model_validate(event_payload(model))
    assert event.cost_role == "charge"
    assert event.charge_id == "account:request"
    assert model.model_validate_json(event.model_dump_json()) == event
    if model in (SdkExecution, SdkExecutionEvent):
        assert event.cost_usd is None
    else:
        assert event.token_cost_usd is None
    with pytest.raises(ValidationError, match="requires charge_id"):
        model.model_validate(event_payload(model, charge_id=None))
    with pytest.raises(ValidationError):
        model.model_validate(event_payload(model, cost_role="summary"))
    with pytest.raises(ValidationError):
        model.model_validate(event_payload(model, cost_role="legacy_unknown"))
    amount = "cost_usd" if model in (SdkExecution, SdkExecutionEvent) else "token_cost_usd"
    with pytest.raises(ValidationError, match="including zero"):
        model.model_validate(
            event_payload(model, cost_role="summary", charge_id=None, **{amount: "0"})
        )
    with pytest.raises(ValidationError):
        model.model_validate(event_payload(model, **{amount: "-1"}))


def test_sdk_to_server_ownership_wire_parity():
    payload = event_payload(SdkExecution, cost_usd="0.25", cost_measurement="estimated")
    sdk = SdkExecution.model_validate(payload)
    server = SdkExecutionEvent.model_validate(sdk.model_dump(mode="json"))
    assert server.model_dump(mode="json") == sdk.model_dump(mode="json")
