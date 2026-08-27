from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from release.live_evaluation.coordinator import ActionRecorder, Phase
from release.live_evaluation.cross_cutting_gates import (
    CheckCommandResult,
    CrossCuttingSources,
    EvidenceFirstCrossCuttingGateExecutor,
    PlaywrightProductionResult,
)
from release.live_evaluation.evidence import EvidenceStore
from release.live_evaluation.reconciliation import ReconciliationResult


def _recorder(store: EvidenceStore, step: str = "cross-cutting") -> ActionRecorder:
    return ActionRecorder(store, step_id=step, command_sequence=1)


def _write_playwright_result(root: Path, *, criteria: list[dict[str, object]]) -> None:
    root.mkdir(parents=True)
    (root / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\nactual-pixels")
    (root / "run.webm").write_bytes(b"\x1aE\xdf\xa3actual-video")
    (root / "report.html").write_text("<html><body>Playwright report</body></html>")
    for name in ("axe.json", "network.json", "console.json", "keyboard.json"):
        (root / name).write_text(json.dumps({"status": "passed", "source": "playwright"}))
    (root / "results.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "completed": True,
                "criteria": criteria,
                "artifacts": [
                    {"source": "shot.png", "destination": "screenshots/shot.png"},
                    {"source": "run.webm", "destination": "videos/run.webm"},
                    {
                        "source": "report.html",
                        "destination": "playwright-report/index.html",
                    },
                    {"source": "axe.json", "destination": "accessibility/axe.json"},
                    {"source": "network.json", "destination": "network/summary.json"},
                    {"source": "console.json", "destination": "console/browser.json"},
                    {"source": "keyboard.json", "destination": "console/keyboard.json"},
                ],
            }
        )
    )


def _reconciliation(store: EvidenceStore, *, passed: bool = True) -> ReconciliationResult:
    event = store.append_event(
        "campaign.reconciliation.completed",
        {"passed": passed, "discrepancies": [] if passed else [{"code": "mismatch"}]},
    )
    return ReconciliationResult(
        passed=passed,
        audit_total_usd=Decimal("0.01"),
        local_total_usd=Decimal("0.01"),
        regulus_total_usd=Decimal("0.01"),
        failure_tax_usd=Decimal("0"),
        discrepancies=(),
        criteria=(),
    ), f"events.ndjson#{event}"


def test_ui_pass_requires_and_ingests_actual_referenced_playwright_artifacts(
    tmp_path: Path,
) -> None:
    store = EvidenceStore(tmp_path / "bundle")
    source = tmp_path / "playwright"
    _write_playwright_result(
        source,
        criteria=[
            {
                "criterion_id": "ui.empty-canvas-authoring",
                "status": "pass",
                "test_id": "studio authoring",
                "evidence": ["screenshots/shot.png", "console/browser.json"],
            }
        ],
    )
    executor = EvidenceFirstCrossCuttingGateExecutor(
        store,
        CrossCuttingSources(playwright_root=source),
    )

    result = executor.execute(
        phase=Phase.CROSS_CUTTING,
        criterion_ids=("ui.empty-canvas-authoring", "ui.node-placement"),
        recorder=_recorder(store),
    )

    by_id = {item.criterion_id: item for item in result.criteria}
    assert by_id["ui.empty-canvas-authoring"].status == "pass"
    assert "screenshots/shot.png" in by_id["ui.empty-canvas-authoring"].evidence
    assert by_id["ui.node-placement"].status == "blocked"
    assert (store.root / "screenshots/shot.png").is_file()
    assert (store.root / "playwright-report/index.html").is_file()


def test_final_files_remain_blocked_and_executor_does_not_finalize_or_seal(
    tmp_path: Path,
) -> None:
    store = EvidenceStore(tmp_path / "bundle")
    executor = EvidenceFirstCrossCuttingGateExecutor(store, CrossCuttingSources())

    result = executor.execute(
        phase=Phase.CROSS_CUTTING,
        criterion_ids=(
            "evidence.acceptance",
            "evidence.report",
            "evidence.sha256-checksums",
        ),
        recorder=_recorder(store),
    )

    assert {item.status for item in result.criteria} == {"blocked"}
    assert not (store.root / "acceptance.json").exists()
    assert not (store.root / "report.md").exists()
    assert not (store.root / "SHA256SUMS").exists()


def test_reconciliation_criteria_are_consumed_exactly_not_reasserted(
    tmp_path: Path,
) -> None:
    store = EvidenceStore(tmp_path / "bundle")
    reconciliation, reference = _reconciliation(store)
    from release.live_evaluation.coordinator import CriterionResult

    reconciliation = ReconciliationResult(
        passed=True,
        audit_total_usd=reconciliation.audit_total_usd,
        local_total_usd=reconciliation.local_total_usd,
        regulus_total_usd=reconciliation.regulus_total_usd,
        failure_tax_usd=reconciliation.failure_tax_usd,
        discrepancies=(),
        criteria=(
            CriterionResult(
                "economics.reconciled-totals", "pass", (reference,)
            ),
        ),
    )
    executor = EvidenceFirstCrossCuttingGateExecutor(
        store,
        CrossCuttingSources(reconciliation=reconciliation),
    )

    result = executor.execute(
        phase=Phase.CROSS_CUTTING,
        criterion_ids=(
            "economics.reconciled-totals",
            "economics.one-event-per-noncache-call",
        ),
        recorder=_recorder(store),
    )

    assert result.criteria[0].status == "pass"
    assert result.criteria[0].evidence == (reference,)
    assert result.criteria[1].status == "blocked"


def test_check_never_runs_until_every_prior_durable_criterion_passed(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "bundle")
    called = []
    executor = EvidenceFirstCrossCuttingGateExecutor(
        store,
        CrossCuttingSources(),
        check_runner=lambda: called.append(True)
        or CheckCommandResult(
            argv=("zeroth", "check"),
            working_directory=tmp_path,
            exit_code=0,
            stdout='{"status":"pass"}',
            stderr="",
            verdict="pass",
        ),
    )

    result = executor.execute(
        phase=Phase.CHECK,
        criterion_ids=("check.after-workflow-gates",),
        recorder=_recorder(store, "check"),
    )

    assert result.criteria[0].status == "fail"
    assert called == []
    assert result.criteria[0].evidence[0].startswith("events.ndjson#")


def test_handoff_passes_only_from_ingested_nonempty_documents(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "bundle")
    reconciliation, _ = _reconciliation(store)
    handoff = tmp_path / "handoff"
    handoff.mkdir()
    (handoff / "discrepancies.md").write_text("# Discrepancies\n\nNone after reconciliation.\n")
    (handoff / "rollback.md").write_text("# Execution and rollback\n\nRollback command and owner.\n")
    (handoff / "project-model.md").write_text("# Project model\n\nRuntime flow and risks.\n")
    executor = EvidenceFirstCrossCuttingGateExecutor(
        store,
        CrossCuttingSources(
            reconciliation=reconciliation,
            handoff_documents={
                "handoff.discrepancy-register": handoff / "discrepancies.md",
                "handoff.execution-and-rollback-instructions": handoff / "rollback.md",
                "handoff.project-model-updated": handoff / "project-model.md",
            }
        ),
    )

    result = executor.execute(
        phase=Phase.CROSS_CUTTING,
        criterion_ids=(
            "handoff.discrepancy-register",
            "handoff.execution-and-rollback-instructions",
            "handoff.project-model-updated",
        ),
        recorder=_recorder(store),
    )

    assert {item.status for item in result.criteria} == {"pass"}
    assert (store.root / "handoff/discrepancies.md").is_file()


def test_handoff_is_blocked_without_reconciliation_even_when_documents_exist(
    tmp_path: Path,
) -> None:
    store = EvidenceStore(tmp_path / "bundle")
    source = tmp_path / "discrepancies.md"
    source.write_text("# Discrepancies\n\nNone after reconciliation.\n")
    executor = EvidenceFirstCrossCuttingGateExecutor(
        store,
        CrossCuttingSources(
            handoff_documents={"handoff.discrepancy-register": source}
        ),
    )

    result = executor.execute(
        phase=Phase.CROSS_CUTTING,
        criterion_ids=("handoff.discrepancy-register",),
        recorder=_recorder(store),
    )

    assert result.criteria[0].status == "blocked"


def test_check_runs_after_every_workflow_gate_and_persists_command_result(
    tmp_path: Path,
) -> None:
    store = EvidenceStore(tmp_path / "bundle")
    from release.live_evaluation.criteria import original_acceptance_criteria

    for item in original_acceptance_criteria():
        if item.criterion_id.startswith(("workflow1.", "workflow2.", "workflow3.")):
            store.append_event(
                "acceptance.recorded",
                {
                    "criterion_id": item.criterion_id,
                    "evidence": ["events.ndjson#prior"],
                    "note": None,
                    "status": "pass",
                },
            )
    called = []
    executor = EvidenceFirstCrossCuttingGateExecutor(
        store,
        CrossCuttingSources(),
        check_runner=lambda: called.append(True)
        or CheckCommandResult(
            argv=("zeroth", "check"),
            working_directory=tmp_path,
            exit_code=0,
            stdout="Zeroth Check: PASS",
            stderr="",
            verdict="pass",
        ),
    )

    result = executor.execute(
        phase=Phase.CHECK,
        criterion_ids=("check.after-workflow-gates",),
        recorder=_recorder(store, "check"),
    )

    assert result.criteria[0].status == "pass"
    assert called == [True]
    command = store.root / result.criteria[0].evidence[0]
    assert json.loads(command.read_text())["exit_code"] == 0


def test_stop_double_count_is_derived_from_reconciliation_failure(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "bundle")
    event = store.append_event(
        "campaign.reconciliation.completed",
        {"passed": False, "discrepancies": [{"code": "duplicate_cost_event"}]},
    )
    from release.live_evaluation.coordinator import CriterionResult

    reconciliation = ReconciliationResult(
        passed=False,
        audit_total_usd=Decimal("0.01"),
        local_total_usd=Decimal("0.02"),
        regulus_total_usd=Decimal("0.01"),
        failure_tax_usd=Decimal("0"),
        discrepancies=(),
        criteria=(
            CriterionResult(
                "economics.one-event-per-noncache-call",
                "fail",
                (f"events.ndjson#{event}",),
            ),
            CriterionResult(
                "economics.reconciled-totals",
                "fail",
                (f"events.ndjson#{event}",),
            ),
        ),
    )
    executor = EvidenceFirstCrossCuttingGateExecutor(
        store, CrossCuttingSources(reconciliation=reconciliation)
    )

    result = executor.execute(
        phase=Phase.CROSS_CUTTING,
        criterion_ids=("stop.no-economic-double-count",),
        recorder=_recorder(store),
    )

    assert result.criteria[0].status == "fail"
    assert f"events.ndjson#{event}" in result.criteria[0].evidence


def test_handoff_artifact_policy_rejects_secret_shaped_content(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "bundle")
    reconciliation, _ = _reconciliation(store)
    source = tmp_path / "rollback.md"
    source.write_text(
        "# Execution and rollback\n\nRollback with sk-proj-abcdefghijklmnopqrstuv.\n"
    )
    executor = EvidenceFirstCrossCuttingGateExecutor(
        store,
        CrossCuttingSources(
            reconciliation=reconciliation,
            handoff_documents={
                "handoff.execution-and-rollback-instructions": source
            },
        ),
    )

    result = executor.execute(
        phase=Phase.CROSS_CUTTING,
        criterion_ids=(
            "handoff.execution-and-rollback-instructions",
            "stop.no-secret-artifact",
        ),
        recorder=_recorder(store),
    )

    assert result.criteria[0].status == "blocked"
    assert result.criteria[1].status == "fail"
    assert not (store.root / "handoff/execution-and-rollback.md").exists()


def test_playwright_and_reconciliation_sources_are_produced_lazily_once(
    tmp_path: Path,
) -> None:
    store = EvidenceStore(tmp_path / "bundle")
    playwright = tmp_path / "playwright"
    _write_playwright_result(
        playwright,
        criteria=[
            {
                "criterion_id": "ui.node-menu",
                "status": "pass",
                "test_id": "node menu",
                "evidence": ["screenshots/shot.png"],
            }
        ],
    )
    reconciliation, _ = _reconciliation(store)
    calls: list[str] = []
    executor = EvidenceFirstCrossCuttingGateExecutor(
        store,
        CrossCuttingSources(
            playwright_producer=lambda: calls.append("playwright")
            or PlaywrightProductionResult(
                artifact_root=playwright,
                argv=("npm", "exec", "playwright", "test"),
                working_directory=tmp_path,
                exit_code=0,
                stdout="1 passed",
                stderr="",
            ),
            reconciliation_collector=lambda evidence_store: calls.append("reconcile")
            or reconciliation,
        ),
    )
    assert calls == []

    result = executor.execute(
        phase=Phase.CROSS_CUTTING,
        criterion_ids=("ui.node-menu",),
        recorder=_recorder(store),
    )

    assert result.criteria[0].status == "pass"
    assert calls == ["playwright", "reconcile"]
    command_files = tuple((store.root / "commands").glob("*.json"))
    assert len(command_files) == 1
