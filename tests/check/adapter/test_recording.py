from __future__ import annotations

import pytest

from zeroth.check.adapter.recording import RecordingError, record_case
from zeroth.check.tape.storage import RawRecordingStore


class Repository:
    pass


def test_real_graph_recording_requires_consent_and_captures_originating_call(tmp_path) -> None:
    arguments = {
        "action_repository": Repository(),
        "case": "7",
        "scenario_run_id": "logical-1",
        "checkpointer_path": tmp_path / "checkpoint.sqlite",
        "store": RawRecordingStore(tmp_path / ".zeroth/check/recordings"),
    }
    with pytest.raises(RecordingError, match="explicit confirmation"):
        record_case("tests.check.fixtures.targets.recording:build_target", **arguments)

    raw, path = record_case(
        "tests.check.fixtures.targets.recording:build_target",
        **arguments,
        allow_side_effects=True,
    )
    occurrence = raw.tool_occurrences[0]
    assert occurrence.tool_call_id == "call-charge-1"
    assert occurrence.arguments == {"amount": 7}
    assert occurrence.result == {"charged": 7}
    assert occurrence.side_effect == "side_effecting"
    assert raw.safety_trajectory[0].occurrence_id == occurrence.occurrence_id
    assert path.exists()
