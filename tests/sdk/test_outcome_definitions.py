"""Lean clients can declare exactly the success rule accepted by the service."""

import json

import pytest
from pydantic import ValidationError

from zeroth.protocol import OutcomeDefinition
from zeroth.econ.plane.debugger.schemas import OutcomeDefinitionCreate
from tests.sdk.test_client import _recording_client


@pytest.mark.parametrize(
    "operator,target",
    [
        ("equals", True),
        ("not_equals", "cancelled"),
        ("greater_than_or_equal", 7),
    ],
)
def test_outcome_definition_wire_parity_and_client_route(operator, target):
    sdk = OutcomeDefinition(
        workflow_id="invoice",
        workflow_version="v1",
        outcome_type="approval",
        operator=operator,
        target=target,
    )
    assert (
        OutcomeDefinitionCreate.model_validate_json(sdk.model_dump_json()).model_dump()
        == sdk.model_dump()
    )
    received = []
    client = _recording_client(received)
    client.create_outcome_definition(sdk)
    assert received[0].url.path == "/v1/debugger/outcome-definitions"
    assert json.loads(received[0].read()) == sdk.model_dump(mode="json")


@pytest.mark.parametrize("model", [OutcomeDefinition, OutcomeDefinitionCreate])
@pytest.mark.parametrize("target", [float("nan"), float("inf"), float("-inf"), True, "7"])
def test_ordered_rules_require_a_finite_numeric_target(model, target):
    with pytest.raises(ValidationError):
        model(
            workflow_id="invoice",
            workflow_version="v1",
            outcome_type="approval",
            operator="greater_than_or_equal",
            target=target,
        )
