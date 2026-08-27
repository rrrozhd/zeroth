from __future__ import annotations

import pytest

from zeroth.check.replay.usage import usage_complete
from zeroth.check.tape.models import ModelCallObservationV1


def _usage(**changes) -> ModelCallObservationV1:
    data = {
        "occurrence_id": "model-1",
        "provider": "openai",
        "model": "gpt-test",
        "input_tokens": 1,
        "output_tokens": 2,
        "total_tokens": 3,
        "input_details": {},
        "output_details": {},
        "request_fingerprint": "sha256:req",
        "response_fingerprint": "sha256:res",
    } | changes
    return ModelCallObservationV1.model_validate(data)


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": None},
        {"provider": " "},
        {"model": None},
        {"input_tokens": None},
        {"output_tokens": None},
        {"total_tokens": 2},
        {"input_details": None},
        {"output_details": None},
    ],
)
def test_incomplete_usage_table(changes) -> None:
    assert usage_complete(_usage(**changes)) is False


def test_complete_usage_accepts_zero_and_nonzero_counts() -> None:
    assert usage_complete(_usage()) is True
    assert usage_complete(_usage(input_tokens=0, output_tokens=0, total_tokens=0)) is True
