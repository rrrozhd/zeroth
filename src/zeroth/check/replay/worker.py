"""One isolated replay worker and its spawn-safe entrypoint."""

from __future__ import annotations

import os
from multiprocessing.queues import Queue
from pathlib import Path

from zeroth.check.adapter.bindings import CheckBindings
from zeroth.check.adapter.langgraph_recording import LangGraphRecordingHandler
from zeroth.check.adapter.loading import load_target
from zeroth.check.replay.matcher import ReplayMatcher
from zeroth.check.replay.models import ReplayMismatchError, ReplayRunEvidence
from zeroth.check.replay.tools import ReplayToolFactory
from zeroth.check.replay.trajectory import project_trajectory, trajectory_bytes
from zeroth.check.replay.usage import all_usage_complete
from zeroth.check.tape.models import TapeV1
from zeroth.integrations.langgraph import SQLiteActionExecutionRepository


def _exception_type_chain(exc: BaseException) -> str:
    """Return bounded diagnostic classes without leaking exception messages."""
    names: list[str] = []
    current: BaseException | None = exc
    while current is not None and len(names) < 4:
        names.append(type(current).__name__)
        current = current.__cause__ or current.__context__
    return " <- ".join(names)


def run_once(
    *,
    slot: int,
    entrypoint: str,
    tape: TapeV1,
    run_root: Path,
) -> ReplayRunEvidence:
    run_root.mkdir(parents=True, exist_ok=True)
    checkpoint_path = run_root / "checkpoint.sqlite"
    action_path = run_root / "actions.sqlite"
    repository = SQLiteActionExecutionRepository(action_path)
    matcher = ReplayMatcher(tape)
    factory = ReplayToolFactory(matcher)
    replacements = {occurrence.name: factory for occurrence in tape.tool_occurrences}
    bindings = CheckBindings(
        action_repository=repository,
        mode="replay",
        replacements=replacements,
    )
    target = load_target(entrypoint, bindings)
    if target.entrypoint_digest != tape.target_entrypoint_digest:
        raise ValueError("target entrypoint digest differs from approved tape")
    if dict(target.case_input(tape.case_id)) != tape.case_input:
        raise ValueError("target case input differs from approved tape")
    if dict(target.invocation_config(tape.case_id, tape.scenario_run_id)) != tape.invocation_config:
        raise ValueError("target invocation config differs from approved tape")
    handler = LangGraphRecordingHandler(
        registrations=dict(bindings.registrations),
        case_id=tape.case_id,
        scenario_run_id=tape.scenario_run_id,
    )
    target.invoke(
        case=tape.case_id,
        scenario_run_id=tape.scenario_run_id,
        checkpointer_path=checkpoint_path,
        callbacks=(handler,),
    )
    finish = matcher.finish()
    model_calls = list(handler.model_calls)
    trajectory = project_trajectory(model_calls, matcher.observed_occurrences)
    has_side_effect = any(item.side_effect == "side_effecting" for item in tape.tool_occurrences)
    return ReplayRunEvidence(
        slot=slot,
        process_id=os.getpid(),
        checkpoint_path=checkpoint_path,
        action_repository_path=action_path,
        trajectory=trajectory_bytes(trajectory),
        facts=finish.facts,
        usage_complete=all_usage_complete(model_calls),
        action_repository_requested=bindings.action_repository_requested,
        full_check_eligible=has_side_effect and bindings.action_repository_requested,
    )


def worker_main(
    queue: Queue,
    slot: int,
    entrypoint: str,
    tape_bytes: bytes,
    run_root: str,
) -> None:
    try:
        tape = TapeV1.model_validate_json(tape_bytes)
        evidence = run_once(
            slot=slot,
            entrypoint=entrypoint,
            tape=tape,
            run_root=Path(run_root),
        )
    except ReplayMismatchError as exc:
        root = Path(run_root)
        evidence = ReplayRunEvidence(
            slot=slot,
            process_id=os.getpid(),
            checkpoint_path=root / "checkpoint.sqlite",
            action_repository_path=root / "actions.sqlite",
            trajectory=None,
            facts=(exc.fact,),
            usage_complete=False,
            action_repository_requested=False,
            full_check_eligible=False,
        )
    except BaseException as exc:
        root = Path(run_root)
        evidence = ReplayRunEvidence(
            slot=slot,
            process_id=os.getpid(),
            checkpoint_path=root / "checkpoint.sqlite",
            action_repository_path=root / "actions.sqlite",
            trajectory=None,
            facts=(),
            usage_complete=False,
            action_repository_requested=False,
            full_check_eligible=False,
            infrastructure_error=_exception_type_chain(exc),
        )
    queue.put(evidence)
