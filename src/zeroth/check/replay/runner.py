"""Exactly-three fresh-process replay orchestration."""

from __future__ import annotations

import multiprocessing
from pathlib import Path
from queue import Empty

from zeroth.check.replay.comparison import compare_three
from zeroth.check.replay.models import ReplayBatch, ReplayRunEvidence
from zeroth.check.replay.trajectory import trajectory_bytes
from zeroth.check.replay.worker import worker_main
from zeroth.check.tape.models import TapeV1


def run_three(
    entrypoint: str,
    tape: TapeV1,
    *,
    state_root: str | Path,
    timeout_seconds: float = 15.0,
) -> ReplayBatch:
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    root = Path(state_root).resolve()
    processes = [
        context.Process(
            target=worker_main,
            args=(queue, slot, entrypoint, tape.canonical_bytes(), str(root / f"run-{slot}")),
        )
        for slot in range(1, 4)
    ]
    for process in processes:
        process.start()
    timed_out: set[int] = set()
    for slot, process in enumerate(processes, start=1):
        process.join(timeout_seconds)
        if process.is_alive():
            timed_out.add(slot)
            process.terminate()
            process.join(2)

    received: dict[int, ReplayRunEvidence] = {}
    for _ in range(3 - len(timed_out)):
        try:
            evidence = queue.get(timeout=2)
        except Empty:
            break
        received[evidence.slot] = evidence
    for slot in range(1, 4):
        if slot not in received:
            run_root = root / f"run-{slot}"
            received[slot] = ReplayRunEvidence(
                slot=slot,
                process_id=processes[slot - 1].pid or -1,
                checkpoint_path=run_root / "checkpoint.sqlite",
                action_repository_path=run_root / "actions.sqlite",
                trajectory=None,
                facts=(),
                usage_complete=False,
                action_repository_requested=False,
                full_check_eligible=False,
                infrastructure_error="WorkerTimeout" if slot in timed_out else "WorkerNoEvidence",
            )
    runs = tuple(received[slot] for slot in range(1, 4))
    baseline = trajectory_bytes(tape.safety_trajectory)
    candidates = [
        run.trajectory if run.trajectory is not None else f"invalid-slot-{run.slot}".encode()
        for run in runs
    ]
    return ReplayBatch(
        runs=runs,
        quorum=compare_three(baseline, candidates),
        invalid_slots=tuple(run.slot for run in runs if run.infrastructure_error is not None),
    )
