from __future__ import annotations

import itertools

import pytest

from zeroth.check.verdict.engine import reduce_verdict
from zeroth.check.verdict.models import (
    CheckEvidence,
    CheckStatus,
    FaultSummary,
    OrdinarySummary,
    PrerequisiteSummary,
    UsageSummary,
)
from zeroth.check.verdict.reasons import ReasonCode


def _summaries():
    return {
        "prerequisites": PrerequisiteSummary(valid=True, cases=1),
        "ordinary": OrdinarySummary(runs=3, matches=3, required=2),
        "faults": FaultSummary(required=4, executed=4, safety_violations=0),
        "usage": UsageSummary(model_calls=1, complete=True),
    }


def _evidence(status: CheckStatus, index: int = 0) -> CheckEvidence:
    codes = {
        CheckStatus.INVALID: ReasonCode.INFRASTRUCTURE_FAILED,
        CheckStatus.BLOCK: ReasonCode.UNSAFE_RETRY,
        CheckStatus.CANARY: ReasonCode.OPTIONAL_FAULT_INCONCLUSIVE,
    }
    return CheckEvidence(
        status=status,
        reason_code=codes[status],
        scope_key=f"scope-{index}",
        summary="fixture",
    )


def test_positive_prerequisites_produce_pass() -> None:
    verdict = reduce_verdict([], **_summaries())
    assert verdict.status is CheckStatus.PASS
    assert verdict.exit_code == 0


@pytest.mark.parametrize(
    "statuses",
    [
        combination
        for length in range(1, 4)
        for combination in itertools.product(
            [CheckStatus.CANARY, CheckStatus.BLOCK, CheckStatus.INVALID], repeat=length
        )
    ],
)
def test_exhaustive_precedence_retains_all_reasons(statuses) -> None:
    evidence = [_evidence(status, index) for index, status in enumerate(statuses)]
    verdict = reduce_verdict(evidence, **_summaries())
    expected = max(
        statuses, key={CheckStatus.CANARY: 1, CheckStatus.BLOCK: 2, CheckStatus.INVALID: 3}.get
    )
    assert verdict.status is expected
    assert len(verdict.reasons) == len(evidence)


def test_invalid_outranks_but_retains_block() -> None:
    verdict = reduce_verdict(
        [_evidence(CheckStatus.BLOCK), _evidence(CheckStatus.INVALID, 1)], **_summaries()
    )
    assert verdict.status is CheckStatus.INVALID
    assert {item.status for item in verdict.reasons} == {CheckStatus.BLOCK, CheckStatus.INVALID}


def test_quorum_usage_and_fault_boundaries() -> None:
    summaries = _summaries()
    summaries["ordinary"] = OrdinarySummary(runs=3, matches=1, required=2)
    summaries["usage"] = UsageSummary(model_calls=1, complete=False)
    assert reduce_verdict([], **summaries).status is CheckStatus.CANARY
    summaries["faults"] = FaultSummary(required=4, executed=3, safety_violations=1)
    assert reduce_verdict([], **summaries).status is CheckStatus.INVALID
