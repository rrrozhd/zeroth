from __future__ import annotations

import json
from pathlib import Path

import pytest

from release.live_evaluation.coordinator import (
    ActionRecorder,
    CampaignCoordinator,
    CampaignPlan,
    CampaignStep,
    CriterionResult,
    Phase,
    StepResult,
)
from release.live_evaluation.evidence import AcceptanceCriterion, CorrelationIds, EvidenceStore


def _criterion(criterion_id: str) -> AcceptanceCriterion:
    return AcceptanceCriterion(criterion_id, "not_run")


def _pass(criterion_id: str):
    def execute(recorder: ActionRecorder) -> StepResult:
        evidence = recorder.record_api_result(
            method="POST",
            path=f"/evaluation/{criterion_id}",
            status_code=200,
            metadata={},
            correlation=CorrelationIds(operation_id=criterion_id),
        )
        return StepResult((CriterionResult(criterion_id, "pass", (evidence,)),))

    return execute


def _full_plan() -> CampaignPlan:
    criteria = tuple(
        _criterion(item)
        for item in (
            "control.ready",
            "workflow1.happy-1",
            "workflow1.happy-2",
            "workflow1.happy-3",
            "workflow1.negative-timeout",
            "workflow2.happy-1",
            "workflow2.happy-2",
            "workflow2.happy-3",
            "workflow2.negative-cancellation",
            "workflow3.happy-1",
            "workflow3.happy-2",
            "workflow3.happy-3",
            "workflow3.negative-rejection",
            "ui.keyboard",
            "audit.signed",
            "economics.reconciled",
            "stop.no-secret-artifact",
            "check.after-workflow-gates",
        )
    )
    steps = tuple(
        CampaignStep(
            step_id=item.criterion_id,
            phase=Phase.for_criterion(item.criterion_id),
            criterion_ids=(item.criterion_id,),
            execute=_pass(item.criterion_id),
        )
        for item in criteria
    )
    return CampaignPlan(criteria=criteria, steps=steps)


def test_plan_requires_all_three_happy_repetitions_and_every_negative_case() -> None:
    plan = _full_plan()
    missing = tuple(step for step in plan.steps if step.step_id != "workflow2.happy-3")

    with pytest.raises(ValueError, match="workflow2.happy-3"):
        CampaignPlan(criteria=plan.criteria, steps=missing)

    duplicate = plan.steps + (plan.steps[0],)
    with pytest.raises(ValueError, match="registered exactly once"):
        CampaignPlan(criteria=plan.criteria, steps=duplicate)


def test_campaign_runs_phases_in_order_and_check_only_after_workflows(
    tmp_path: Path,
) -> None:
    coordinator = CampaignCoordinator(EvidenceStore(tmp_path), _full_plan())

    summary = coordinator.run()

    assert summary.completed
    assert summary.check_ran
    events = [json.loads(line) for line in (tmp_path / "events.ndjson").read_text().splitlines()]
    started = [
        event["data"]["step_id"] for event in events if event["type"] == "campaign.step.started"
    ]
    assert started.index("workflow1.happy-3") < started.index("workflow2.happy-1")
    assert started.index("workflow3.happy-3") < started.index("ui.keyboard")
    assert started[-1] == "check.after-workflow-gates"
    assert not (tmp_path / "acceptance.json").exists()
    EvidenceStore(tmp_path).append_event("report.preparation.started", {})


def test_failed_step_halts_and_blocks_all_remaining_criteria(tmp_path: Path) -> None:
    plan = _full_plan()

    def fail(recorder: ActionRecorder) -> StepResult:
        evidence = recorder.record_ui_action(
            action="verify-provider-credential",
            outcome="validation failed",
            metadata={"status": "rejected"},
        )
        return StepResult((CriterionResult("control.ready", "fail", (evidence,), "gate failed"),))

    steps = (
        CampaignStep("control.ready", Phase.CONTROL, ("control.ready",), fail),
        *plan.steps[1:],
    )
    coordinator = CampaignCoordinator(EvidenceStore(tmp_path), CampaignPlan(plan.criteria, steps))

    summary = coordinator.run()

    assert summary.halted_by == "control.ready"
    assert not summary.check_ran
    from release.live_evaluation.ledger import CampaignLedger

    statuses = {
        row.criterion_id: row.status
        for row in CampaignLedger(EvidenceStore(tmp_path), plan.criteria).resolved_criteria()
    }
    assert statuses["control.ready"] == "fail"
    assert statuses["workflow1.happy-1"] == "blocked"
    assert statuses["check.after-workflow-gates"] == "blocked"


def test_stop_condition_failure_immediately_halts_without_later_action(
    tmp_path: Path,
) -> None:
    criteria = (
        _criterion("stop.no-secret-artifact"),
        _criterion("ui.must-not-run"),
    )
    calls: list[str] = []

    def fail_stop(recorder: ActionRecorder) -> StepResult:
        evidence = recorder.record_ui_action(action="scan-evidence", outcome="unsafe", metadata={})
        return StepResult((CriterionResult("stop.no-secret-artifact", "fail", (evidence,)),))

    plan = CampaignPlan(
        criteria,
        (
            CampaignStep("scan", Phase.CONTROL, ("stop.no-secret-artifact",), fail_stop),
            CampaignStep(
                "later",
                Phase.CROSS_CUTTING,
                ("ui.must-not-run",),
                lambda recorder: calls.append("later") or _pass("ui.must-not-run")(recorder),
            ),
        ),
    )

    summary = CampaignCoordinator(EvidenceStore(tmp_path), plan).run()

    assert summary.halted_by == "stop.no-secret-artifact"
    assert calls == []


def test_check_registration_requires_all_three_workflows() -> None:
    criteria = tuple(
        _criterion(item)
        for item in (
            "workflow1.happy-1",
            "workflow1.happy-2",
            "workflow1.happy-3",
            "check.after-workflow-gates",
        )
    )
    steps = tuple(
        CampaignStep(
            item.criterion_id,
            Phase.for_criterion(item.criterion_id),
            (item.criterion_id,),
            _pass(item.criterion_id),
        )
        for item in criteria
    )

    with pytest.raises(ValueError, match="all three workflows"):
        CampaignPlan(criteria, steps)


def test_resume_commits_durable_action_without_reexecuting_step(tmp_path: Path) -> None:
    criteria = (_criterion("control.ready"),)
    calls = 0

    def execute(recorder: ActionRecorder) -> StepResult:
        nonlocal calls
        calls += 1
        evidence = recorder.record_api_result(
            method="GET", path="/health", status_code=200, metadata={"state": "ready"}
        )
        return StepResult((CriterionResult("control.ready", "pass", (evidence,)),))

    plan = CampaignPlan(
        criteria,
        (CampaignStep("ready", Phase.CONTROL, ("control.ready",), execute),),
    )
    store = EvidenceStore(tmp_path)
    first = CampaignCoordinator(store, plan, interrupt_after_action=True)

    with pytest.raises(RuntimeError, match="simulated interruption"):
        first.run()

    resumed = CampaignCoordinator(EvidenceStore(tmp_path), plan).run()

    assert resumed.completed
    assert calls == 1
    actions = [
        json.loads(line)
        for line in (tmp_path / "events.ndjson").read_text().splitlines()
        if json.loads(line)["type"] == "campaign.step.action_completed"
    ]
    assert len(actions) == 1


def test_resume_finishes_partially_committed_multi_criterion_step(tmp_path: Path) -> None:
    criteria = (_criterion("control.one"), _criterion("control.two"))
    calls = 0

    def execute(recorder: ActionRecorder) -> StepResult:
        nonlocal calls
        calls += 1
        first = recorder.record_api_result(
            method="GET", path="/first", status_code=200, metadata={}
        )
        second = recorder.record_api_result(
            method="GET", path="/second", status_code=200, metadata={}
        )
        return StepResult(
            (
                CriterionResult("control.one", "pass", (first,)),
                CriterionResult("control.two", "pass", (second,)),
            )
        )

    plan = CampaignPlan(
        criteria,
        (CampaignStep("control", Phase.CONTROL, ("control.one", "control.two"), execute),),
    )
    store = EvidenceStore(tmp_path)
    recorder = ActionRecorder(store, step_id="control", command_sequence=1)
    result = execute(recorder)
    store.append_event(
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
            "phase": "CONTROL",
            "step_id": "control",
        },
    )
    from release.live_evaluation.ledger import CampaignLedger

    CampaignLedger(store, criteria).record(
        "control.one", "pass", evidence=result.criteria[0].evidence
    )

    summary = CampaignCoordinator(EvidenceStore(tmp_path), plan).run()

    assert summary.completed
    assert calls == 1
    ledger = CampaignLedger(EvidenceStore(tmp_path), criteria)
    assert {row.status for row in ledger.resolved_criteria()} == {"pass"}


def test_action_recorder_persists_sanitized_command_api_and_ui_results(
    tmp_path: Path,
) -> None:
    recorder = ActionRecorder(EvidenceStore(tmp_path), step_id="durability", command_sequence=1)

    command = recorder.record_command_result(
        name="probe",
        argv=("probe", "--safe"),
        working_directory=tmp_path,
        exit_code=0,
        stdout="ok",
        stderr="",
    )
    api = recorder.record_api_result(
        method="POST", path="/v1/probe", status_code=202, metadata={"request_id": "r-1"}
    )
    ui = recorder.record_ui_action(
        action="click-publish", outcome="dialog opened", metadata={"surface": "studio"}
    )

    assert command == "commands/0001-probe.json"
    assert api.startswith("events.ndjson#")
    assert ui.startswith("events.ndjson#")
    assert json.loads((tmp_path / command).read_text())["exit_code"] == 0
    ui_event = next(
        event
        for event in EvidenceStore(tmp_path).read_events()
        if event["type"] == "campaign.ui.completed"
    )
    assert ui_event["correlation"]["ui_action_id"]


def test_exception_is_recorded_as_failure_without_unsafe_exception_text(
    tmp_path: Path,
) -> None:
    criteria = (_criterion("control.ready"),)

    def explode(recorder: ActionRecorder) -> StepResult:
        raise RuntimeError("Bearer abcdefghijklmnopqrstuvwxyz")

    plan = CampaignPlan(
        criteria,
        (CampaignStep("ready", Phase.CONTROL, ("control.ready",), explode),),
    )

    summary = CampaignCoordinator(EvidenceStore(tmp_path), plan).run()

    assert summary.halted_by == "control.ready"
    assert "abcdefghijklmnopqrstuvwxyz" not in (tmp_path / "events.ndjson").read_text()


def test_terminal_failed_campaign_retains_halt_reason_after_restart(tmp_path: Path) -> None:
    criteria = (_criterion("control.ready"),)

    def fail(recorder: ActionRecorder) -> StepResult:
        evidence = recorder.record_ui_action(action="gate", outcome="failed", metadata={})
        return StepResult((CriterionResult("control.ready", "fail", (evidence,)),))

    plan = CampaignPlan(
        criteria,
        (CampaignStep("ready", Phase.CONTROL, ("control.ready",), fail),),
    )
    first = CampaignCoordinator(EvidenceStore(tmp_path), plan).run()
    resumed = CampaignCoordinator(EvidenceStore(tmp_path), plan).run()

    assert first.halted_by == "control.ready"
    assert resumed.halted_by == "control.ready"


def test_fabricated_evidence_reference_is_rejected(tmp_path: Path) -> None:
    criteria = (_criterion("control.ready"),)
    plan = CampaignPlan(
        criteria,
        (
            CampaignStep(
                "ready",
                Phase.CONTROL,
                ("control.ready",),
                lambda recorder: StepResult(
                    (CriterionResult("control.ready", "pass", ("events.ndjson#invented",)),)
                ),
            ),
        ),
    )

    with pytest.raises(ValueError, match="does not exist"):
        CampaignCoordinator(EvidenceStore(tmp_path), plan).run()
