"""All outcome contracts preserve maturity, unknown observations and source time."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from zeroth.econ.instrumentation.schemas import OutcomeEvent as RuntimeOutcome
from zeroth.econ.plane.cloud.schemas import SdkOutcomeEvent
from zeroth.econ.plane.instrumentation.schemas import OutcomeEventCreate
from zeroth.protocol import OutcomeEvent as SdkOutcome

MODELS = [RuntimeOutcome, SdkOutcomeEvent, OutcomeEventCreate, SdkOutcome]


def payload(model, maturity="final", value=True, when=datetime(2026, 9, 6, tzinfo=UTC)):
    if model in (SdkOutcome, SdkOutcomeEvent):
        return {
            "workflow": "invoice",
            "run_id": "run",
            "accepted": value,
            "occurred_at": when,
            "maturity": maturity,
        }
    return {
        "capability_id": "invoice",
        "execution_id": "event",
        "outcome_type": "approval",
        "outcome_value": value,
        "outcome_timestamp": when,
        "maturity": maturity,
    }


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize(
    "state,value", [("unknown", True), ("provisional", None), ("final", False), ("withdrawn", None)]
)
def test_outcome_maturity_roundtrip(model, state, value):
    data = payload(model, state, value)
    event = model.model_validate(data)
    assert event.maturity == state
    assert model.model_validate_json(event.model_dump_json()) == event


@pytest.mark.parametrize("model", MODELS)
def test_maturity_value_and_source_time_contract(model):
    with pytest.raises(ValidationError, match="require an observation"):
        model.model_validate(payload(model, "final", None))
    with pytest.raises(ValidationError, match="cannot carry an observation"):
        model.model_validate(payload(model, "withdrawn", True))
    with pytest.raises(ValidationError, match="aware"):
        model.model_validate(payload(model, when=datetime(2026, 9, 6)))
    when = datetime(2026, 9, 6, 5, tzinfo=timezone(timedelta(hours=5)))
    event = model.model_validate(payload(model, when=when))
    timestamp = (
        event.occurred_at if model in (SdkOutcome, SdkOutcomeEvent) else event.outcome_timestamp
    )
    assert timestamp == datetime(2026, 9, 6, tzinfo=UTC)
    assert timestamp.utcoffset() == timedelta(0)


def test_sdk_server_wire_parity_and_generic_timestamp_aliases():
    sdk = SdkOutcome.model_validate(payload(SdkOutcome, "provisional", None))
    assert (
        SdkOutcomeEvent.model_validate_json(sdk.model_dump_json()).model_dump() == sdk.model_dump()
    )
    data = payload(OutcomeEventCreate)
    with pytest.raises(ValidationError, match="same instant"):
        OutcomeEventCreate.model_validate(
            {**data, "occurred_at": data["outcome_timestamp"] + timedelta(seconds=1)}
        )
