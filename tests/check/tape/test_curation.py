from __future__ import annotations

import json

import pytest

from zeroth.check.tape.curation import CurationError, curate_raw_recording
from zeroth.check.tape.models import RawRecordingV1, TapeV1

from .test_models import _payload


def test_curates_scrubbed_approved_tape_and_recomputes_identity(tmp_path) -> None:
    payload = _payload()
    occurrence = payload["tool_occurrences"][0].model_copy(
        update={"arguments": {"api_key": "sk-proj-abcdefghijklmnopqrstuvwxyz123456"}}
    )
    # Re-seal a valid raw artifact containing the secret-bearing identity.
    from zeroth.check.tape.normalization import action_identity_v1, argument_fingerprint

    fingerprint = argument_fingerprint(dict(occurrence.arguments))
    occurrence = occurrence.model_copy(
        update={
            "argument_fingerprint": fingerprint,
            "action_identity": action_identity_v1(
                case_id="payment-case",
                scenario_run_id="logical-run-1",
                tool_name=occurrence.name,
                input_schema_digest=occurrence.input_schema_digest,
                tool_call_id=occurrence.tool_call_id,
                argument_fingerprint=fingerprint,
            ),
        }
    )
    payload["tool_occurrences"] = [occurrence]
    raw = RawRecordingV1.seal(**payload)
    raw_path = tmp_path / "raw.json"
    raw_path.write_bytes(raw.canonical_bytes())
    output = tmp_path / "checks/tapes/payment.json"

    result = curate_raw_recording(
        raw_path,
        output=output,
        reviewer_id="reviewer@example.com",
        approved_at="2026-08-19T18:00:00Z",
    )

    tape = TapeV1.model_validate_json(output.read_bytes())
    assert result.tape == tape
    assert tape.identity_changed_by_scrubbing is True
    assert "sk-proj" not in output.read_text()
    assert result.manifest.finding_count >= 1


def test_rejects_missing_reviewer_tampered_raw_and_overwrite(tmp_path) -> None:
    raw = RawRecordingV1.seal(**_payload())
    raw_path = tmp_path / "raw.json"
    raw_path.write_bytes(raw.canonical_bytes())
    output = tmp_path / "tape.json"
    with pytest.raises(CurationError, match="reviewer"):
        curate_raw_recording(raw_path, output=output, reviewer_id=" ")
    tampered = json.loads(raw_path.read_bytes())
    tampered["target_entrypoint_digest"] = "sha256:tampered"
    raw_path.write_text(json.dumps(tampered))
    with pytest.raises(CurationError, match="raw recording"):
        curate_raw_recording(raw_path, output=output, reviewer_id="reviewer")

    raw_path.write_bytes(raw.canonical_bytes())
    curate_raw_recording(raw_path, output=output, reviewer_id="reviewer")
    with pytest.raises(CurationError, match="exists"):
        curate_raw_recording(raw_path, output=output, reviewer_id="reviewer")
