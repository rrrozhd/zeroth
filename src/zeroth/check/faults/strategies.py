"""Event-driven implementations of the mandatory V1 fault strategies."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import threading
from contextlib import suppress
from pathlib import Path
from typing import Any

from zeroth.check.faults.controller import validate_fault_execution
from zeroth.check.faults.models import FaultEventKind, FaultName, FaultResult, FaultSpec
from zeroth.check.faults.store import FaultEvidenceStore
from zeroth.check.tape.models import ToolOccurrenceV1
from zeroth.integrations.langgraph import (
    ActionExecutionState,
    SideEffectClass,
    SQLiteActionExecutionRepository,
    ToolAction,
    ToolGovernanceContext,
    ToolIdentity,
)


def _action(spec: FaultSpec, occurrence: ToolOccurrenceV1) -> ToolAction:
    return ToolAction(
        identity=ToolIdentity(name=occurrence.name, fingerprint=spec.action_identity),
        arguments=dict(occurrence.arguments),
        side_effect=SideEffectClass.SIDE_EFFECTING,
        tool_call_id=occurrence.tool_call_id,
    )


def _context(spec: FaultSpec) -> ToolGovernanceContext:
    return ToolGovernanceContext(
        tenant_id="zeroth-check",
        principal_id="fault-harness",
        run_id=spec.case_id,
        thread_id=f"check:{spec.case_id}",
    )


def duplicate_delivery(
    spec: FaultSpec,
    occurrence: ToolOccurrenceV1,
    *,
    action_path: Path,
    evidence: FaultEvidenceStore,
) -> FaultResult:
    repository_a = SQLiteActionExecutionRepository(action_path)
    repository_b = SQLiteActionExecutionRepository(action_path)
    action = _action(spec, occurrence)
    context = _context(spec)
    marker_written = threading.Event()
    release = threading.Event()
    failures: list[BaseException] = []
    evidence.append(spec, FaultEventKind.INJECTION_ARMED, process_role="controller")

    def first_delivery() -> None:
        try:
            claim = repository_a.begin_once(action, context)
            if not claim.may_execute:
                raise RuntimeError("first delivery did not acquire the action claim")
            evidence.append(spec, FaultEventKind.EFFECT_MARKER_WRITTEN, process_role="worker_a")
            marker_written.set()
            if not release.wait(5):
                raise TimeoutError("duplicate delivery barrier expired")
            repository_a.complete(claim, occurrence.result)
            evidence.append(spec, FaultEventKind.RECEIPT_STORED, process_role="worker_a")
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=first_delivery, name="check-duplicate-a")
    worker.start()
    if marker_written.wait(5):
        duplicate = repository_b.begin_once(action, context)
        if duplicate.may_execute:
            evidence.append(spec, FaultEventKind.EFFECT_MARKER_WRITTEN, process_role="worker_b")
        evidence.append(spec, FaultEventKind.INJECTION_REACHED, process_role="worker_b")
    release.set()
    worker.join(5)
    if not failures and not worker.is_alive():
        replay = repository_b.begin_once(action, context)
        if replay.record.state is ActionExecutionState.COMPLETED:
            repository_b.replay_or_raise(replay.record)
            evidence.append(spec, FaultEventKind.RECOVERY_REACHED, process_role="replay")
            evidence.append(spec, FaultEventKind.RUN_TERMINAL, process_role="controller")
    return validate_fault_execution(spec, evidence)


def timeout_after_effect(
    spec: FaultSpec,
    occurrence: ToolOccurrenceV1,
    *,
    action_path: Path,
    evidence: FaultEvidenceStore,
) -> FaultResult:
    repository = SQLiteActionExecutionRepository(action_path)
    action = _action(spec, occurrence)
    context = _context(spec)
    evidence.append(spec, FaultEventKind.INJECTION_ARMED, process_role="controller")
    claim = repository.begin_once(action, context)
    evidence.append(spec, FaultEventKind.EFFECT_MARKER_WRITTEN, process_role="worker")
    error = TimeoutError("injected after effect marker")
    evidence.append(spec, FaultEventKind.INJECTION_REACHED, process_role="worker")
    record = repository.mark_ambiguous(claim, error, close_claim=True)
    if record.state is ActionExecutionState.AMBIGUOUS:
        evidence.append(spec, FaultEventKind.AMBIGUITY_OBSERVED, process_role="worker")
    retry = repository.begin_once(action, context)
    if retry.may_execute:
        evidence.append(spec, FaultEventKind.EFFECT_MARKER_WRITTEN, process_role="retry")
    else:
        evidence.append(spec, FaultEventKind.RECOVERY_REACHED, process_role="retry")
    evidence.append(spec, FaultEventKind.RUN_TERMINAL, process_role="controller")
    return validate_fault_execution(spec, evidence)


async def _cancellation_scenario(
    spec: FaultSpec,
    occurrence: ToolOccurrenceV1,
    action_path: Path,
    evidence: FaultEvidenceStore,
) -> None:
    repository = SQLiteActionExecutionRepository(action_path)
    action = _action(spec, occurrence)
    context = _context(spec)
    reached = asyncio.Event()
    never = asyncio.Event()
    evidence.append(spec, FaultEventKind.INJECTION_ARMED, process_role="controller")

    async def delivery() -> None:
        claim = repository.begin_once(action, context)
        evidence.append(spec, FaultEventKind.EFFECT_MARKER_WRITTEN, process_role="worker")
        evidence.append(spec, FaultEventKind.INJECTION_REACHED, process_role="worker")
        reached.set()
        try:
            await never.wait()
        except asyncio.CancelledError as error:
            evidence.append(spec, FaultEventKind.CANCELLATION_OBSERVED, process_role="worker")
            record = repository.mark_ambiguous(claim, error, close_claim=True)
            if record.state is ActionExecutionState.AMBIGUOUS:
                evidence.append(spec, FaultEventKind.AMBIGUITY_OBSERVED, process_role="worker")
            raise

    task = asyncio.create_task(delivery())
    await asyncio.wait_for(reached.wait(), 5)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    retry = repository.begin_once(action, context)
    if retry.may_execute:
        evidence.append(spec, FaultEventKind.EFFECT_MARKER_WRITTEN, process_role="resume")
    else:
        evidence.append(spec, FaultEventKind.RECOVERY_REACHED, process_role="resume")
    evidence.append(spec, FaultEventKind.RUN_TERMINAL, process_role="controller")


def cancellation_after_effect(
    spec: FaultSpec,
    occurrence: ToolOccurrenceV1,
    *,
    action_path: Path,
    evidence: FaultEvidenceStore,
) -> FaultResult:
    asyncio.run(_cancellation_scenario(spec, occurrence, action_path, evidence))
    return validate_fault_execution(spec, evidence)


def error_before_effect(
    spec: FaultSpec,
    occurrence: ToolOccurrenceV1,
    *,
    action_path: Path,
    evidence: FaultEvidenceStore,
) -> FaultResult:
    repository = SQLiteActionExecutionRepository(action_path)
    action = _action(spec, occurrence)
    context = _context(spec)
    evidence.append(spec, FaultEventKind.INJECTION_ARMED, process_role="controller")
    claim = repository.begin_once(action, context)
    evidence.append(spec, FaultEventKind.INJECTION_REACHED, process_role="worker")
    repository.fail_pre_effect(claim, RuntimeError("injected before effect"))
    retry = repository.begin_once(action, context)
    if retry.may_execute:
        evidence.append(spec, FaultEventKind.EFFECT_MARKER_WRITTEN, process_role="retry")
        repository.complete(retry, occurrence.result)
        evidence.append(spec, FaultEventKind.RECOVERY_REACHED, process_role="retry")
        evidence.append(spec, FaultEventKind.RUN_TERMINAL, process_role="controller")
    return validate_fault_execution(spec, evidence)


def _restart_worker_a(
    spec_data: dict[str, Any], occurrence_data: dict[str, Any], action_path: str, evidence_path: str
) -> None:
    spec = FaultSpec.model_validate(spec_data)
    occurrence = ToolOccurrenceV1.model_validate(occurrence_data)
    evidence = FaultEvidenceStore(evidence_path)
    repository = SQLiteActionExecutionRepository(action_path)
    claim = repository.begin_once(_action(spec, occurrence), _context(spec))
    evidence.append(spec, FaultEventKind.INJECTION_ARMED, process_role="worker_a")
    evidence.append(spec, FaultEventKind.EFFECT_MARKER_WRITTEN, process_role="worker_a")
    repository.complete(claim, occurrence.result)
    evidence.append(spec, FaultEventKind.RECEIPT_STORED, process_role="worker_a")
    evidence.append(spec, FaultEventKind.INJECTION_REACHED, process_role="worker_a")
    os._exit(17)


def _restart_worker_b(
    spec_data: dict[str, Any], occurrence_data: dict[str, Any], action_path: str, evidence_path: str
) -> None:
    spec = FaultSpec.model_validate(spec_data)
    occurrence = ToolOccurrenceV1.model_validate(occurrence_data)
    evidence = FaultEvidenceStore(evidence_path)
    repository = SQLiteActionExecutionRepository(action_path)
    evidence.append(spec, FaultEventKind.RESUME_STARTED, process_role="worker_b")
    replay = repository.begin_once(_action(spec, occurrence), _context(spec))
    if replay.record.state is ActionExecutionState.COMPLETED:
        repository.replay_or_raise(replay.record)
        evidence.append(spec, FaultEventKind.RECOVERY_REACHED, process_role="worker_b")
        evidence.append(spec, FaultEventKind.RUN_TERMINAL, process_role="worker_b")


def restart_after_receipt(
    spec: FaultSpec,
    occurrence: ToolOccurrenceV1,
    *,
    action_path: Path,
    evidence: FaultEvidenceStore,
) -> FaultResult:
    context = multiprocessing.get_context("spawn")
    arguments = (
        spec.model_dump(),
        occurrence.model_dump(),
        str(action_path),
        str(evidence.path),
    )
    worker_a = context.Process(target=_restart_worker_a, args=arguments)
    worker_a.start()
    worker_a.join(10)
    if worker_a.is_alive():
        worker_a.terminate()
        worker_a.join(2)
        return validate_fault_execution(spec, evidence)
    evidence.append(spec, FaultEventKind.PROCESS_EXITED, process_role="controller")
    worker_b = context.Process(target=_restart_worker_b, args=arguments)
    worker_b.start()
    worker_b.join(10)
    if worker_b.is_alive():
        worker_b.terminate()
        worker_b.join(2)
    return validate_fault_execution(spec, evidence)


STRATEGIES = {
    FaultName.DUPLICATE_DELIVERY: duplicate_delivery,
    FaultName.TIMEOUT_AFTER_EFFECT: timeout_after_effect,
    FaultName.CANCELLATION_AFTER_EFFECT: cancellation_after_effect,
    FaultName.RESTART_AFTER_RECEIPT: restart_after_receipt,
    FaultName.ERROR_BEFORE_EFFECT: error_before_effect,
}
