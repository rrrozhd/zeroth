from release.live_evaluation.criteria import original_acceptance_criteria


def test_original_acceptance_catalog_is_complete_unique_and_initially_not_run() -> None:
    criteria = original_acceptance_criteria()
    identifiers = [criterion.criterion_id for criterion in criteria]

    assert len(criteria) >= 80
    assert len(identifiers) == len(set(identifiers))
    assert all(criterion.status == "not_run" for criterion in criteria)
    assert {
        "control.audit-signed",
        "control.budget-concurrency",
        "evidence.playwright-html-report",
        "evidence.sha256-checksums",
        "workflow1.happy-1",
        "workflow1.happy-2",
        "workflow1.happy-3",
        "workflow1.negative-excessive-revision",
        "workflow2.happy-1",
        "workflow2.happy-2",
        "workflow2.happy-3",
        "workflow2.negative-child-pause-partial-collection",
        "workflow2.negative-child-failure-partial-collection",
        "workflow3.happy-1",
        "workflow3.happy-2",
        "workflow3.happy-3",
        "workflow3.negative-timeout-after-commit",
        "ui.viewport-390x844",
        "ui.zoom-200-percent",
        "audit.signed-chain-verifies",
        "economics.reconciled-totals",
        "stop.no-secret-artifact",
        "stop.no-ambiguous-auto-retry",
        "check.after-workflow-gates",
        "handoff.project-model-updated",
    }.issubset(identifiers)
