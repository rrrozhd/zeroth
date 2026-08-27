from __future__ import annotations

import pytest

from zeroth.check.replay.matcher import ReplayMatcher
from zeroth.check.replay.models import MismatchClassification, MismatchReason, ReplayMismatchError
from zeroth.check.tape.models import RawRecordingV1, TapeV1, ToolOccurrenceV1
from zeroth.check.tape.normalization import action_identity_v1, argument_fingerprint

from ..tape.test_models import _payload


def _tape(*, second: bool = False, missing_result: bool = False) -> TapeV1:
    payload = _payload()
    if missing_result:
        payload["tool_occurrences"] = [
            payload["tool_occurrences"][0].model_copy(
                update={"result_available": False, "result": None, "error_type": "TimeoutError"}
            )
        ]
    if second:
        arguments = {"query": "status"}
        fingerprint = argument_fingerprint(arguments)
        payload["tool_occurrences"].append(
            ToolOccurrenceV1(
                occurrence_id="tool-2",
                name="lookup",
                input_schema_digest="sha256:lookup-schema",
                tool_call_id="call-2",
                arguments=arguments,
                argument_fingerprint=fingerprint,
                side_effect="read_only",
                result_available=True,
                result=None,
                action_identity=action_identity_v1(
                    case_id="payment-case",
                    scenario_run_id="logical-run-1",
                    tool_name="lookup",
                    input_schema_digest="sha256:lookup-schema",
                    tool_call_id="call-2",
                    argument_fingerprint=fingerprint,
                ),
            )
        )
    raw = RawRecordingV1.seal(**payload)
    return TapeV1.seal_from_raw(
        raw,
        scrubber_version="scrubber.v1",
        secret_rules_version="secret_rules.v1",
        reviewer_id="reviewer",
        approved_at="2026-08-19T18:00:00Z",
        identity_changed_by_scrubbing=False,
    )


def test_returns_recorded_result_including_json_null_and_allows_reordering() -> None:
    matcher = ReplayMatcher(_tape(second=True))
    assert (
        matcher.call(
            name="lookup",
            schema_digest="sha256:lookup-schema",
            tool_call_id="call-2",
            arguments={"query": "status"},
        )
        is None
    )
    assert matcher.call(
        name="charge_card",
        schema_digest="sha256:schema",
        tool_call_id="call-1",
        arguments={"currency": "USD", "amount": 100},
    ) == {"receipt": "ok"}
    assert matcher.finish().facts[0].reason == MismatchReason.CHANGED_ORDER


@pytest.mark.parametrize(
    ("changes", "reason", "classification"),
    [
        ({"name": "unknown"}, MismatchReason.UNKNOWN_TOOL, MismatchClassification.BLOCK),
        (
            {"schema_digest": "sha256:changed"},
            MismatchReason.SCHEMA_DIGEST_MISMATCH,
            MismatchClassification.INVALID,
        ),
        (
            {"tool_call_id": "changed"},
            MismatchReason.TOOL_CALL_ID_MISMATCH,
            MismatchClassification.BLOCK,
        ),
        (
            {"arguments": {"amount": 101, "currency": "USD"}},
            MismatchReason.ARGUMENT_MISMATCH,
            MismatchClassification.BLOCK,
        ),
    ],
)
def test_fail_closed_mismatch_table(changes, reason, classification) -> None:
    matcher = ReplayMatcher(_tape())
    call = {
        "name": "charge_card",
        "schema_digest": "sha256:schema",
        "tool_call_id": "call-1",
        "arguments": {"amount": 100, "currency": "USD"},
    } | changes
    with pytest.raises(ReplayMismatchError) as caught:
        matcher.call(**call)
    assert caught.value.fact.reason == reason
    assert caught.value.fact.classification == classification
    assert "amount" not in str(caught.value.fact)


def test_missing_result_is_invalid_and_duplicate_effect_is_block() -> None:
    missing = ReplayMatcher(_tape(missing_result=True))
    call = {
        "name": "charge_card",
        "schema_digest": "sha256:schema",
        "tool_call_id": "call-1",
        "arguments": {"amount": 100, "currency": "USD"},
    }
    with pytest.raises(ReplayMismatchError) as caught:
        missing.call(**call)
    assert caught.value.fact.classification == MismatchClassification.INVALID

    matcher = ReplayMatcher(_tape())
    matcher.call(**call)
    with pytest.raises(ReplayMismatchError) as caught:
        matcher.call(**call)
    assert caught.value.fact.reason == MismatchReason.DUPLICATE_SIDE_EFFECT


def test_early_end_is_an_ordinary_mismatch() -> None:
    finish = ReplayMatcher(_tape()).finish()
    assert finish.facts[0].classification == MismatchClassification.ORDINARY_MISMATCH
