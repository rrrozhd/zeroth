"""Raw Check recording orchestration."""

from __future__ import annotations

from pathlib import Path

from zeroth.check.adapter.bindings import CheckBindings
from zeroth.check.adapter.langgraph_recording import LangGraphRecordingHandler
from zeroth.check.adapter.loading import load_target
from zeroth.check.replay.trajectory import project_trajectory, trajectory_digest
from zeroth.check.tape.models import RawRecordingV1
from zeroth.check.tape.storage import RawRecordingStore


class RecordingError(RuntimeError):
    """A valid raw recording could not be captured."""


def record_case(
    entrypoint: str,
    *,
    action_repository: object,
    case: str,
    scenario_run_id: str,
    checkpointer_path: str | Path,
    store: RawRecordingStore,
    allow_side_effects: bool = False,
) -> tuple[RawRecordingV1, Path]:
    bindings = CheckBindings(action_repository=action_repository)
    target = load_target(entrypoint, bindings)
    registrations = dict(bindings.registrations)
    if (
        any(item.side_effect == "side_effecting" for item in registrations.values())
        and not allow_side_effects
    ):
        raise RecordingError("side-effecting recording requires explicit confirmation")
    handler = LangGraphRecordingHandler(
        registrations=registrations,
        case_id=case,
        scenario_run_id=scenario_run_id,
    )
    target.invoke(
        case=case,
        scenario_run_id=scenario_run_id,
        checkpointer_path=checkpointer_path,
        callbacks=(handler,),
    )
    occurrences = list(handler.tool_occurrences)
    model_calls = list(handler.model_calls)
    trajectory = project_trajectory(model_calls, occurrences)
    case_input = dict(target.case_input(case))
    invocation_config = dict(target.invocation_config(case, scenario_run_id))
    raw = RawRecordingV1.seal(
        normalization_version="normalization.v1",
        action_identity_version="action_identity.v1",
        case_id=case,
        scenario_run_id=scenario_run_id,
        adapter={"name": "langgraph", "version": "1"},
        target_entrypoint_digest=target.entrypoint_digest,
        case_input=case_input,
        invocation_config=invocation_config,
        model_calls=model_calls,
        tool_occurrences=occurrences,
        safety_trajectory=trajectory,
        trajectory_digest=trajectory_digest(trajectory),
    )
    return raw, store.write(raw)
