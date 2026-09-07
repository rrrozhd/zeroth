"""All original execution contracts agree on representable owned-charge amounts."""

from datetime import UTC, datetime
from decimal import Decimal

from pydantic import ValidationError
import pytest

from zeroth.econ.instrumentation.schemas import ExecutionEvent as RuntimeEvent
from zeroth.econ.plane.cloud.schemas import SdkExecutionEvent
from zeroth.econ.plane.instrumentation.schemas import ExecutionEventCreate
from zeroth.protocol import ExecutionEvent as SdkEvent


def payload(model, amount, role="charge", component="token_cost_usd"):
    base = {"cost_role": role, "charge_id": "owned" if role == "charge" else None}
    if model in (SdkEvent, SdkExecutionEvent):
        return {
            **base,
            "workflow": "invoice",
            "step": "provider",
            "run_id": "run",
            "cost_usd": amount,
        }
    return {
        **base,
        "execution_id": "event",
        "capability_id": "invoice",
        "implementation_id": "v1",
        "timestamp": datetime(2026, 9, 6, tzinfo=UTC),
        "model_version": "model",
        component: amount,
    }


@pytest.mark.parametrize("model", [SdkEvent, SdkExecutionEvent, RuntimeEvent, ExecutionEventCreate])
@pytest.mark.parametrize(
    "amount", [None, 0, 42, "0.00000001", "9999999999.12345678", Decimal(".12345678")]
)
def test_original_contracts_preserve_representable_amounts(model, amount):
    event = model.model_validate(payload(model, amount))
    field = "cost_usd" if model in (SdkEvent, SdkExecutionEvent) else "token_cost_usd"
    assert getattr(event, field) == (Decimal(amount) if amount is not None else None)
    assert model.model_validate_json(event.model_dump_json()) == event


@pytest.mark.parametrize("model", [SdkEvent, SdkExecutionEvent, RuntimeEvent, ExecutionEventCreate])
@pytest.mark.parametrize("amount", ["0.000000001", "10000000000", "NaN", "-0.01", 0.1])
def test_original_owned_contracts_reject_lossy_or_invalid_amounts(model, amount):
    with pytest.raises(ValidationError):
        model.model_validate(payload(model, amount))


@pytest.mark.parametrize("model", [SdkEvent, SdkExecutionEvent, RuntimeEvent, ExecutionEventCreate])
def test_representable_legacy_floats_remain_compatible(model):
    event = model.model_validate(payload(model, 0.1, role="legacy_unknown"))
    field = "cost_usd" if model in (SdkEvent, SdkExecutionEvent) else "token_cost_usd"
    assert getattr(event, field) == Decimal("0.1")


@pytest.mark.parametrize("model", [RuntimeEvent, ExecutionEventCreate])
@pytest.mark.parametrize("component", ["token_cost_usd", "tool_cost_usd", "compute_cost_usd"])
def test_every_generic_component_uses_the_same_precision_and_existing_sign_rules(model, component):
    event = model.model_validate(payload(model, "9999999999.12345678", component=component))
    assert getattr(event, component) == Decimal("9999999999.12345678")
    with pytest.raises(ValidationError):
        model.model_validate(payload(model, "0.000000001", component=component))
    legacy = model.model_validate(
        payload(model, "-0.01", role="legacy_unknown", component=component)
    )
    assert getattr(legacy, component) == Decimal("-0.01")
