from __future__ import annotations

from zeroth.check.adapter.bindings import CheckBindings
from zeroth.check.adapter.loading import load_target
from zeroth.check.replay.trajectory import project_trajectory, trajectory_digest
from zeroth.check.tape.models import RawRecordingV1, TapeV1, ToolOccurrenceV1
from zeroth.check.tape.normalization import action_identity_v1, argument_fingerprint


class Repository:
    pass


def replay_tape() -> TapeV1:
    bindings = CheckBindings(action_repository=Repository())
    target = load_target("tests.check.fixtures.targets.replay:build_target", bindings)
    registration = bindings.registrations["charge"]
    arguments = {"amount": 7}
    fingerprint = argument_fingerprint(arguments)
    occurrence = ToolOccurrenceV1(
        occurrence_id="tool-0001",
        name="charge",
        input_schema_digest=registration.input_schema_digest,
        tool_call_id="call-charge-1",
        arguments=arguments,
        argument_fingerprint=fingerprint,
        side_effect="side_effecting",
        result_available=True,
        result={"charged": 7},
        action_identity=action_identity_v1(
            case_id="7",
            scenario_run_id="logical-1",
            tool_name="charge",
            input_schema_digest=registration.input_schema_digest,
            tool_call_id="call-charge-1",
            argument_fingerprint=fingerprint,
        ),
    )
    trajectory = project_trajectory([], [occurrence])
    raw = RawRecordingV1.seal(
        normalization_version="normalization.v1",
        action_identity_version="action_identity.v1",
        case_id="7",
        scenario_run_id="logical-1",
        adapter={"name": "langgraph", "version": "1"},
        target_entrypoint_digest=target.entrypoint_digest,
        case_input=dict(target.case_input("7")),
        invocation_config=dict(target.invocation_config("7", "logical-1")),
        model_calls=[],
        tool_occurrences=[occurrence],
        safety_trajectory=trajectory,
        trajectory_digest=trajectory_digest(trajectory),
    )
    return TapeV1.seal_from_raw(
        raw,
        scrubber_version="scrubber.v1",
        secret_rules_version="secret_rules.v1",
        reviewer_id="reviewer",
        approved_at="2026-08-19T18:00:00Z",
        identity_changed_by_scrubbing=False,
    )
