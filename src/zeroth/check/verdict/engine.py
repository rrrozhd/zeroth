"""Pure precedence reduction from evidence and positive prerequisites."""

from __future__ import annotations

from collections.abc import Iterable

from zeroth.check.verdict.models import (
    STATUS_EXIT,
    CheckEvidence,
    CheckStatus,
    CheckVerdict,
    FaultSummary,
    OrdinarySummary,
    PrerequisiteSummary,
    ReportMetadata,
    UsageSummary,
)
from zeroth.check.verdict.reasons import ReasonCode

_PRECEDENCE = {
    CheckStatus.PASS: 0,
    CheckStatus.CANARY: 1,
    CheckStatus.BLOCK: 2,
    CheckStatus.INVALID: 3,
}


def _fact(status: CheckStatus, code: ReasonCode, scope: str, summary: str) -> CheckEvidence:
    return CheckEvidence(status=status, reason_code=code, scope_key=scope, summary=summary)


def reduce_verdict(
    evidence: Iterable[CheckEvidence],
    *,
    prerequisites: PrerequisiteSummary,
    ordinary: OrdinarySummary,
    faults: FaultSummary,
    usage: UsageSummary,
    report: ReportMetadata | None = None,
) -> CheckVerdict:
    reasons = list(evidence)
    if not prerequisites.valid:
        reasons.append(
            _fact(
                CheckStatus.INVALID,
                ReasonCode.CONFIG_INVALID,
                "prerequisites",
                "Prerequisites failed",
            )
        )
    if faults.executed < faults.required:
        reasons.append(
            _fact(
                CheckStatus.INVALID,
                ReasonCode.FAULT_NOT_OBSERVED,
                "faults",
                "Not every mandatory fault produced injection and recovery evidence",
            )
        )
    if faults.safety_violations:
        reasons.append(
            _fact(
                CheckStatus.BLOCK,
                ReasonCode.DUPLICATE_EFFECT,
                "faults",
                "A safety fault re-executed an effect",
            )
        )
    if ordinary.matches < ordinary.required:
        reasons.append(
            _fact(
                CheckStatus.CANARY,
                ReasonCode.ORDINARY_QUORUM_MISSED,
                "ordinary",
                "Fewer than two ordinary trajectories matched",
            )
        )
    if not usage.complete:
        reasons.append(
            _fact(
                CheckStatus.CANARY,
                ReasonCode.USAGE_INCOMPLETE,
                "usage",
                "Model usage evidence is incomplete",
            )
        )
    status = max(
        (item.status for item in reasons), key=_PRECEDENCE.__getitem__, default=CheckStatus.PASS
    )
    ordered = tuple(sorted(reasons, key=lambda item: (item.reason_code.value, item.scope_key)))
    return CheckVerdict(
        status=status,
        exit_code=STATUS_EXIT[status],
        reasons=ordered,
        prerequisites=prerequisites,
        ordinary=ordinary,
        faults=faults,
        usage=usage,
        report=report or ReportMetadata(),
    )
