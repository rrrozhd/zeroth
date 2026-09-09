import pytest
from pydantic import ValidationError

from zeroth.protocol.models import OutcomeEvent


@pytest.mark.parametrize("length", [1, 64])
def test_valid_outcome_type(length):
    event = OutcomeEvent(workflow="test", run_id="run-1", accepted=True, outcome_type="x" * length)
    assert event.outcome_type == "x" * length


@pytest.mark.parametrize("length", [0, 65])
def test_invalid_outcome_type(length):
    with pytest.raises(ValidationError):
        OutcomeEvent(workflow="test", run_id="run-1", accepted=True, outcome_type="x" * length)
