"""Safety-relevant NormalizationV1 trajectory projection."""

from __future__ import annotations

from collections.abc import Iterable

from zeroth.check.replay.usage import usage_complete
from zeroth.check.tape.models import (
    ModelCallObservationV1,
    SafetyTrajectoryEventV1,
    ToolOccurrenceV1,
)
from zeroth.check.tape.normalization import canonical_bytes, sha256_digest


def project_trajectory(
    model_calls: Iterable[ModelCallObservationV1],
    tool_occurrences: Iterable[ToolOccurrenceV1],
    *,
    controls: Iterable[SafetyTrajectoryEventV1] = (),
) -> list[SafetyTrajectoryEventV1]:
    events: list[SafetyTrajectoryEventV1] = []
    for model in model_calls:
        complete = usage_complete(model)
        events.append(
            SafetyTrajectoryEventV1(
                event_type="model_call",
                occurrence_id=model.occurrence_id,
                fingerprint=sha256_digest(
                    {"provider": model.provider, "model": model.model, "usage_complete": complete}
                ),
                state="complete" if complete else "usage_incomplete",
            )
        )
    for tool in tool_occurrences:
        events.append(
            SafetyTrajectoryEventV1(
                event_type="tool_request",
                occurrence_id=tool.occurrence_id,
                fingerprint=sha256_digest(
                    {
                        "action_identity": tool.action_identity,
                        "side_effect": tool.side_effect,
                        "argument_fingerprint": tool.argument_fingerprint,
                    }
                ),
            )
        )
        events.append(
            SafetyTrajectoryEventV1(
                event_type="tool_terminal",
                occurrence_id=tool.occurrence_id,
                fingerprint=sha256_digest(
                    {
                        "result_available": tool.result_available,
                        "result": tool.result,
                        "error_type": tool.error_type,
                    }
                ),
                state="completed" if tool.result_available else "error",
            )
        )
    events.extend(controls)
    return events


def trajectory_bytes(events: Iterable[SafetyTrajectoryEventV1]) -> bytes:
    return canonical_bytes([event.model_dump(mode="json") for event in events])


def trajectory_digest(events: Iterable[SafetyTrajectoryEventV1]) -> str:
    return sha256_digest([event.model_dump(mode="json") for event in events])
