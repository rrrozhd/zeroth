from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from zeroth.check.tape.models import (
    ModelCallObservationV1,
    RawRecordingV1,
    SafetyTrajectoryEventV1,
    TapeV1,
    ToolOccurrenceV1,
)
from zeroth.check.tape.normalization import action_identity_v1, argument_fingerprint, sha256_digest


def _tool() -> ToolOccurrenceV1:
    arguments = {"amount": 100, "currency": "USD"}
    fingerprint = argument_fingerprint(arguments)
    return ToolOccurrenceV1(
        occurrence_id="tool-1",
        name="charge_card",
        input_schema_digest="sha256:schema",
        tool_call_id="call-1",
        arguments=arguments,
        argument_fingerprint=fingerprint,
        side_effect="side_effecting",
        result_available=True,
        result={"receipt": "ok"},
        action_identity=action_identity_v1(
            case_id="payment-case",
            scenario_run_id="logical-run-1",
            tool_name="charge_card",
            input_schema_digest="sha256:schema",
            tool_call_id="call-1",
            argument_fingerprint=fingerprint,
        ),
    )


def _payload() -> dict[str, object]:
    trajectory = [
        SafetyTrajectoryEventV1(
            event_type="tool_result", occurrence_id="tool-1", fingerprint="sha256:event"
        )
    ]
    return {
        "normalization_version": "normalization.v1",
        "action_identity_version": "action_identity.v1",
        "case_id": "payment-case",
        "scenario_run_id": "logical-run-1",
        "adapter": {"name": "langgraph", "version": "1"},
        "target_entrypoint_digest": "sha256:target",
        "case_input": {"request": "charge"},
        "invocation_config": {"configurable": {"thread_id": "logical-thread"}},
        "model_calls": [
            ModelCallObservationV1(
                occurrence_id="model-1",
                provider="openai",
                model="gpt-test",
                input_tokens=1,
                output_tokens=2,
                total_tokens=3,
                input_details={"cached": 0},
                output_details={"reasoning": 0},
                request_fingerprint="sha256:req",
                response_fingerprint="sha256:res",
            )
        ],
        "tool_occurrences": [_tool()],
        "safety_trajectory": trajectory,
        "trajectory_digest": sha256_digest([event.model_dump(mode="json") for event in trajectory]),
    }


def test_raw_recording_digest_round_trips_and_detects_tampering() -> None:
    raw = RawRecordingV1.seal(**_payload())
    loaded = RawRecordingV1.model_validate_json(raw.canonical_bytes())
    assert loaded == raw
    tampered = raw.model_dump(mode="json")
    tampered["target_entrypoint_digest"] = "sha256:other"
    with pytest.raises(ValidationError, match="source_digest"):
        RawRecordingV1.model_validate(tampered)


def test_raw_forbids_approval_fields_and_tape_rejects_raw() -> None:
    raw = RawRecordingV1.seal(**_payload())
    with pytest.raises(ValidationError):
        RawRecordingV1.model_validate(raw.model_dump() | {"reviewer_id": "reviewer"})
    with pytest.raises(ValidationError):
        TapeV1.model_validate(raw.model_dump())


def test_tape_requires_approval_metadata_and_verifies_digest() -> None:
    raw = RawRecordingV1.seal(**_payload())
    tape = TapeV1.seal_from_raw(
        raw,
        scrubber_version="scrubber.v1",
        secret_rules_version="secret_rules.v1",
        reviewer_id="reviewer@example.com",
        approved_at="2026-08-19T18:00:00Z",
        identity_changed_by_scrubbing=False,
    )
    assert TapeV1.model_validate_json(tape.canonical_bytes()) == tape
    tampered = tape.model_dump(mode="json")
    tampered["reviewer_id"] = "someone-else"
    with pytest.raises(ValidationError, match="curated_content_digest"):
        TapeV1.model_validate(tampered)


def test_missing_tool_result_is_explicit_and_side_effect_is_required() -> None:
    data = _tool().model_dump()
    data.update(result_available=False, result=None, error_type="TimeoutError")
    assert ToolOccurrenceV1.model_validate(data).result_available is False
    del data["side_effect"]
    with pytest.raises(ValidationError):
        ToolOccurrenceV1.model_validate(data)


def test_usage_counts_and_details_are_complete() -> None:
    data = _payload()["model_calls"][0].model_dump()
    data["input_tokens"] = -1
    with pytest.raises(ValidationError):
        ModelCallObservationV1.model_validate(data)


def test_golden_tape_round_trips_byte_for_byte() -> None:
    path = Path(__file__).parents[1] / "fixtures/tapes/tape-v1.json"
    expected = path.read_bytes().rstrip(b"\n")
    tape = TapeV1.model_validate_json(expected)
    assert tape.canonical_bytes() == expected
