"""Complete model-usage evidence validation."""

from __future__ import annotations

from zeroth.check.tape.models import ModelCallObservationV1


def usage_complete(observation: ModelCallObservationV1) -> bool:
    if not isinstance(observation.provider, str) or not observation.provider.strip():
        return False
    if not isinstance(observation.model, str) or not observation.model.strip():
        return False
    counts = (observation.input_tokens, observation.output_tokens, observation.total_tokens)
    if any(type(value) is not int or value < 0 for value in counts):
        return False
    if observation.input_details is None or observation.output_details is None:
        return False
    assert observation.input_tokens is not None
    assert observation.output_tokens is not None
    assert observation.total_tokens is not None
    return observation.total_tokens >= observation.input_tokens + observation.output_tokens


def all_usage_complete(observations: list[ModelCallObservationV1]) -> bool:
    return all(usage_complete(item) for item in observations)
