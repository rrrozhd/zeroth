from pathlib import Path

import pytest

from release.live_evaluation.evidence import AcceptanceCriterion, EvidenceStore
from release.live_evaluation.ledger import CampaignHaltedError, CampaignLedger


def _criteria() -> tuple[AcceptanceCriterion, ...]:
    return (
        AcceptanceCriterion("workflow1.happy-1", "not_run"),
        AcceptanceCriterion("workflow2.happy-1", "not_run"),
        AcceptanceCriterion("workflow3.happy-1", "not_run"),
        AcceptanceCriterion("stop.no-secret-artifact", "not_run"),
        AcceptanceCriterion("check.after-workflow-gates", "not_run"),
    )


def test_stop_failure_halts_and_finalization_blocks_every_remaining_gate(tmp_path: Path) -> None:
    ledger = CampaignLedger(EvidenceStore(tmp_path), _criteria())
    ledger.record("workflow1.happy-1", "pass", evidence=("events.ndjson#one",))
    ledger.record("stop.no-secret-artifact", "fail", evidence=("events.ndjson#stop",))

    assert ledger.halted
    with pytest.raises(CampaignHaltedError):
        ledger.record("workflow2.happy-1", "pass", evidence=("events.ndjson#two",))

    by_id = {item.criterion_id: item for item in ledger.resolved_criteria()}
    assert by_id["stop.no-secret-artifact"].status == "fail"
    assert by_id["workflow2.happy-1"].status == "blocked"
    assert by_id["check.after-workflow-gates"].status == "blocked"


def test_check_is_ineligible_until_every_workflow_gate_passes(tmp_path: Path) -> None:
    ledger = CampaignLedger(EvidenceStore(tmp_path), _criteria())
    assert not ledger.may_run_check
    for criterion_id in ("workflow1.happy-1", "workflow2.happy-1", "workflow3.happy-1"):
        ledger.record(criterion_id, "pass", evidence=(f"events.ndjson#{criterion_id}",))
    assert ledger.may_run_check


def test_pass_or_fail_without_evidence_is_refused(tmp_path: Path) -> None:
    ledger = CampaignLedger(EvidenceStore(tmp_path), _criteria())
    with pytest.raises(ValueError, match="evidence reference"):
        ledger.record("workflow1.happy-1", "pass")
