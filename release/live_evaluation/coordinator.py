"""Fail-closed, resumable orchestration for the staged live-evaluation campaign.

This module coordinates pluggable actions.  It deliberately contains no provider,
browser, or product API client: workflow-specific executors perform those actions
and use :class:`ActionRecorder` to persist their sanitized outcomes.
"""

from __future__ import annotations

import json
import re
import uuid
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Literal

from .evidence import AcceptanceCriterion, CorrelationIds, EvidenceStore
from .ledger import CampaignLedger

ResultStatus = Literal["pass", "fail", "blocked"]
_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class Phase(IntEnum):
    """Required campaign phase order."""

    CONTROL = 1
    WORKFLOW_1 = 2
    WORKFLOW_2 = 3
    WORKFLOW_3 = 4
    CROSS_CUTTING = 5
    CHECK = 6

    @classmethod
    def for_criterion(cls, criterion_id: str) -> Phase:
        prefix = criterion_id.split(".", 1)[0]
        return {
            "control": cls.CONTROL,
            "workflow1": cls.WORKFLOW_1,
            "workflow2": cls.WORKFLOW_2,
            "workflow3": cls.WORKFLOW_3,
            "ui": cls.CROSS_CUTTING,
            "audit": cls.CROSS_CUTTING,
            "economics": cls.CROSS_CUTTING,
            "stop": cls.CROSS_CUTTING,
            "check": cls.CHECK,
        }.get(prefix, cls.CROSS_CUTTING)


@dataclass(frozen=True)
class CriterionResult:
    criterion_id: str
    status: ResultStatus
    evidence: tuple[str, ...]
    note: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"pass", "fail", "blocked"}:
            raise ValueError(f"invalid criterion result status: {self.status}")
        if self.status in {"pass", "fail"} and not self.evidence:
            raise ValueError("pass/fail result requires durable evidence")


@dataclass(frozen=True)
class StepResult:
    criteria: tuple[CriterionResult, ...]


StepExecutor = Callable[["ActionRecorder"], StepResult]


@dataclass(frozen=True)
class CampaignStep:
    step_id: str
    phase: Phase
    criterion_ids: tuple[str, ...]
    execute: StepExecutor

    def __post_init__(self) -> None:
        if not _SAFE_NAME.fullmatch(self.step_id.replace(".", "_")):
            raise ValueError(f"unsafe campaign step identifier: {self.step_id!r}")
        if not self.criterion_ids:
            raise ValueError("campaign step must own at least one criterion")


@dataclass(frozen=True)
class CampaignPlan:
    """Complete registration of acceptance criteria to executable steps."""

    criteria: tuple[AcceptanceCriterion, ...]
    steps: tuple[CampaignStep, ...]

    def __post_init__(self) -> None:
        criterion_ids = [criterion.criterion_id for criterion in self.criteria]
        if len(set(criterion_ids)) != len(criterion_ids):
            raise ValueError("acceptance criteria must be unique")
        registered = [item for step in self.steps for item in step.criterion_ids]
        counts = Counter(registered)
        invalid = sorted(
            criterion_id
            for criterion_id in set(criterion_ids) | set(registered)
            if counts[criterion_id] != 1 or criterion_id not in criterion_ids
        )
        if invalid:
            joined = ", ".join(invalid)
            raise ValueError(f"criteria must be registered exactly once: {joined}")
        for step in self.steps:
            for criterion_id in step.criterion_ids:
                if criterion_id.startswith("stop."):
                    if step.phase == Phase.CHECK:
                        raise ValueError("stop conditions must run before Zeroth Check")
                    continue
                expected = Phase.for_criterion(criterion_id)
                if step.phase != expected:
                    raise ValueError(f"criterion {criterion_id!r} belongs to phase {expected.name}")
        self._validate_workflow_repetitions(set(criterion_ids))
        if "check.after-workflow-gates" in criterion_ids:
            missing_workflows = [
                workflow
                for workflow in ("workflow1", "workflow2", "workflow3")
                if not any(item.startswith(f"{workflow}.") for item in criterion_ids)
            ]
            if missing_workflows:
                raise ValueError("Zeroth Check requires all three workflows in the campaign plan")

    @staticmethod
    def _validate_workflow_repetitions(criterion_ids: set[str]) -> None:
        for workflow in ("workflow1", "workflow2", "workflow3"):
            present = {item for item in criterion_ids if item.startswith(f"{workflow}.")}
            if not present:
                continue
            required = {f"{workflow}.happy-{number}" for number in range(1, 4)}
            missing = sorted(required - present)
            if missing:
                raise ValueError(
                    f"{workflow} requires three happy repetitions: {', '.join(missing)}"
                )


@dataclass(frozen=True)
class CampaignSummary:
    completed: bool
    halted_by: str | None
    check_ran: bool
    completed_steps: tuple[str, ...]


class ActionRecorder:
    """Persist sanitized command/API/UI outcomes and return evidence references."""

    def __init__(self, store: EvidenceStore, *, step_id: str, command_sequence: int) -> None:
        self.store = store
        self.step_id = step_id
        self.command_sequence = command_sequence

    def record_command_result(
        self,
        *,
        name: str,
        argv: Sequence[str],
        working_directory: Path,
        exit_code: int,
        stdout: str,
        stderr: str,
    ) -> str:
        if not _SAFE_NAME.fullmatch(name):
            raise ValueError("command evidence name must be a safe slug")
        path = self.store.record_command(
            sequence=self.command_sequence,
            name=name,
            argv=argv,
            working_directory=working_directory,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
        )
        self.command_sequence += 1
        return path.relative_to(self.store.root).as_posix()

    def record_api_result(
        self,
        *,
        method: str,
        path: str,
        status_code: int,
        metadata: Mapping[str, object],
        correlation: CorrelationIds | None = None,
    ) -> str:
        if not path.startswith("/") or "?" in path or "#" in path:
            raise ValueError("API evidence path must omit origin, query, and fragment")
        safe_metadata = dict(metadata)
        legacy_ids = {
            field: safe_metadata.pop(field)
            for field in (
                "operation_id",
                "run_id",
                "audit_event_id",
                "cost_event_id",
                "provider_request_id",
            )
            if field in safe_metadata
        }
        if legacy_ids:
            supplied = correlation.as_dict() if correlation is not None else {}
            for field, value in legacy_ids.items():
                if field in supplied and supplied[field] != value:
                    raise ValueError(f"conflicting typed correlation identifier: {field}")
                supplied[field] = value
            correlation = CorrelationIds(**supplied)
        event_id = self.store.append_event(
            "campaign.api.completed",
            {
                "metadata": safe_metadata,
                "method": method.upper(),
                "path": path,
                "status_code": status_code,
                "step_id": self.step_id,
            },
            correlation=correlation,
        )
        return f"events.ndjson#{event_id}"

    def record_ui_action(
        self,
        *,
        action: str,
        outcome: str,
        metadata: Mapping[str, object],
        correlation: CorrelationIds | None = None,
    ) -> str:
        values = correlation.as_dict() if correlation is not None else {}
        values.setdefault("ui_action_id", str(uuid.uuid4()))
        event_id = self.store.append_event(
            "campaign.ui.completed",
            {
                "action": action,
                "metadata": dict(metadata),
                "outcome": outcome,
                "step_id": self.step_id,
            },
            correlation=CorrelationIds(**values),
        )
        return f"events.ndjson#{event_id}"


@dataclass(frozen=True)
class _RecoveredState:
    actions: Mapping[str, StepResult]
    committed_steps: tuple[str, ...]


class CampaignCoordinator:
    """Execute a complete campaign plan serially and resume without replaying actions."""

    def __init__(
        self,
        store: EvidenceStore,
        plan: CampaignPlan,
        *,
        interrupt_after_action: bool = False,
        campaign_terminal: bool = True,
        enforce_check_gate: bool = True,
        emit_completion_event: bool = True,
    ) -> None:
        self.store = store
        self.plan = plan
        self._interrupt_after_action = interrupt_after_action
        self._campaign_terminal = campaign_terminal
        self._enforce_check_gate = enforce_check_gate
        self._emit_completion_event = emit_completion_event

    def run(self) -> CampaignSummary:
        terminal = self._terminal_summary()
        if terminal is not None:
            return terminal
        recovered = self._recover()
        ledger = CampaignLedger(self.store, self.plan.criteria)
        actions = dict(recovered.actions)
        completed = list(recovered.committed_steps)
        command_sequence = self._next_command_sequence()
        ordered_steps = sorted(
            enumerate(self.plan.steps), key=lambda pair: (pair[1].phase, pair[0])
        )
        for _, step in ordered_steps:
            if step.step_id in completed:
                continue
            if step.phase == Phase.CHECK and self._enforce_check_gate and not ledger.may_run_check:
                return self._halt(ledger, completed, "check.after-workflow-gates", check_ran=False)
            result = actions.get(step.step_id)
            if result is None:
                self.store.append_event(
                    "campaign.step.started",
                    {"phase": step.phase.name, "step_id": step.step_id},
                )
                recorder = ActionRecorder(
                    self.store,
                    step_id=step.step_id,
                    command_sequence=command_sequence,
                )
                result = self._execute(step, recorder)
                command_sequence = recorder.command_sequence
                self._validate_step_result(step, result)
                self.store.append_event(
                    "campaign.step.action_completed",
                    {
                        "criteria": [
                            {
                                "criterion_id": item.criterion_id,
                                "evidence": list(item.evidence),
                                "note": item.note,
                                "status": item.status,
                            }
                            for item in result.criteria
                        ],
                        "phase": step.phase.name,
                        "step_id": step.step_id,
                    },
                )
                if self._interrupt_after_action:
                    raise RuntimeError("simulated interruption after durable action")
            self._commit_result(ledger, result)
            self.store.append_event("campaign.step.committed", {"step_id": step.step_id})
            completed.append(step.step_id)
            failed = next(
                (item.criterion_id for item in result.criteria if item.status == "fail"),
                None,
            )
            if failed is not None:
                return self._halt(
                    ledger,
                    completed,
                    failed,
                    check_ran=step.phase == Phase.CHECK,
                )
        summary = CampaignSummary(
            completed=True,
            halted_by=None,
            check_ran=any(
                step.phase == Phase.CHECK and step.step_id in completed for step in self.plan.steps
            ),
            completed_steps=tuple(completed),
        )
        if self._emit_completion_event:
            self.store.append_event(
                "campaign.completed" if self._campaign_terminal else "campaign.stage.completed",
                {
                    "check_ran": summary.check_ran,
                    "completed_steps": list(summary.completed_steps),
                },
            )
        return summary

    def _execute(self, step: CampaignStep, recorder: ActionRecorder) -> StepResult:
        try:
            return step.execute(recorder)
        except Exception as exc:  # fail closed; raw exception text is intentionally omitted
            event_id = self.store.append_event(
                "campaign.step.exception",
                {"exception_type": type(exc).__name__, "step_id": step.step_id},
            )
            return StepResult(
                tuple(
                    CriterionResult(
                        criterion_id,
                        "fail" if index == 0 else "blocked",
                        (f"events.ndjson#{event_id}",) if index == 0 else (),
                        "step executor raised; inspect sanitized runtime evidence",
                    )
                    for index, criterion_id in enumerate(step.criterion_ids)
                )
            )

    def _validate_step_result(self, step: CampaignStep, result: StepResult) -> None:
        returned = [item.criterion_id for item in result.criteria]
        if Counter(returned) != Counter(step.criterion_ids):
            raise ValueError(
                f"step {step.step_id!r} must return each registered criterion exactly once"
            )
        for item in result.criteria:
            for reference in item.evidence:
                self._assert_evidence_exists(reference)

    def _assert_evidence_exists(self, reference: str) -> None:
        if reference.startswith("events.ndjson#"):
            event_id = reference.partition("#")[2]
            if not event_id or not any(
                event.get("event_id") == event_id for event in self._events()
            ):
                raise ValueError(f"evidence reference does not exist: {reference}")
            return
        candidate = (self.store.root / reference).resolve(strict=False)
        try:
            candidate.relative_to(self.store.root)
        except ValueError as exc:
            raise ValueError(f"evidence reference escapes campaign root: {reference}") from exc
        if not candidate.is_file():
            raise ValueError(f"evidence reference does not exist: {reference}")

    @staticmethod
    def _commit_result(ledger: CampaignLedger, result: StepResult) -> None:
        durable = {criterion.criterion_id: criterion for criterion in ledger.criteria}
        ordered = sorted(
            result.criteria,
            key=lambda item: item.criterion_id.startswith("stop."),
        )
        for item in ordered:
            current = durable[item.criterion_id]
            if current.status != "not_run":
                if (
                    current.status != item.status
                    or current.evidence != item.evidence
                    or current.note != item.note
                ):
                    raise ValueError(
                        f"durable acceptance disagrees with action for {item.criterion_id}"
                    )
                continue
            ledger.record(
                item.criterion_id,
                item.status,
                evidence=item.evidence,
                note=item.note,
            )

    def _halt(
        self,
        ledger: CampaignLedger,
        completed: list[str],
        failed: str,
        *,
        check_ran: bool,
    ) -> CampaignSummary:
        if not ledger.halted:
            recorded = self._recorded_statuses()
            for criterion in self.plan.criteria:
                if criterion.criterion_id not in recorded:
                    ledger.record(
                        criterion.criterion_id,
                        "blocked",
                        note=f"campaign halted by {failed}",
                    )
        summary = CampaignSummary(
            completed=False,
            halted_by=failed,
            check_ran=check_ran,
            completed_steps=tuple(completed),
        )
        self.store.append_event(
            "campaign.terminated",
            {
                "check_ran": check_ran,
                "completed_steps": list(summary.completed_steps),
                "halted_by": failed,
            },
        )
        return summary

    def _recover(self) -> _RecoveredState:
        actions: dict[str, StepResult] = {}
        committed: list[str] = []
        for event in self._events():
            data = event["data"]
            if event["type"] == "campaign.step.action_completed":
                actions[str(data["step_id"])] = StepResult(
                    tuple(
                        CriterionResult(
                            str(row["criterion_id"]),
                            str(row["status"]),  # type: ignore[arg-type]
                            tuple(row.get("evidence", ())),
                            row.get("note"),
                        )
                        for row in data["criteria"]
                    )
                )
            elif event["type"] == "campaign.step.committed":
                committed.append(str(data["step_id"]))
        return _RecoveredState(actions, tuple(committed))

    def _events(self) -> list[dict[str, object]]:
        path = self.store.root / "events.ndjson"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line]

    def _recorded_statuses(self) -> set[str]:
        return {
            str(event["data"]["criterion_id"])
            for event in self._events()
            if event["type"] == "acceptance.recorded"
        }

    def _next_command_sequence(self) -> int:
        commands = self.store.root / "commands"
        return len(tuple(commands.glob("*.json"))) + 1 if commands.exists() else 1

    def _terminal_summary(self) -> CampaignSummary | None:
        for event in reversed(self._events()):
            data = event["data"]
            if event["type"] == "campaign.completed":
                return CampaignSummary(
                    completed=True,
                    halted_by=None,
                    check_ran=bool(data.get("check_ran")),
                    completed_steps=tuple(data.get("completed_steps", ())),
                )
            if event["type"] == "campaign.terminated":
                return CampaignSummary(
                    completed=False,
                    halted_by=str(data["halted_by"]),
                    check_ran=bool(data.get("check_ran")),
                    completed_steps=tuple(data.get("completed_steps", ())),
                )
        path = self.store.root / "acceptance.json"
        if not path.exists():
            return None
        rows = json.loads(path.read_text())["criteria"]
        failed = next((row["criterion_id"] for row in rows if row["status"] == "fail"), None)
        committed = tuple(
            str(event["data"]["step_id"])
            for event in self._events()
            if event["type"] == "campaign.step.committed"
        )
        return CampaignSummary(
            completed=failed is None and all(row["status"] == "pass" for row in rows),
            halted_by=failed,
            check_ran=any(
                row["criterion_id"] == "check.after-workflow-gates" and row["status"] == "pass"
                for row in rows
            ),
            completed_steps=committed,
        )
