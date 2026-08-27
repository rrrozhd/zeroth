"""Fault occurrence expansion and bounded matrix execution."""

from __future__ import annotations

import re
from pathlib import Path

from zeroth.check.faults.catalog import MANDATORY_FAULTS, validate_additional
from zeroth.check.faults.models import FaultMatrixResult, FaultSpec
from zeroth.check.faults.store import FaultEvidenceStore
from zeroth.check.faults.strategies import STRATEGIES
from zeroth.check.tape.models import TapeV1, ToolOccurrenceV1


def expand_fault_matrix(
    tape: TapeV1, *, additional: list[str] | None = None
) -> tuple[FaultSpec, ...]:
    optional = validate_additional(additional or [])
    unique: dict[str, ToolOccurrenceV1] = {}
    for occurrence in tape.tool_occurrences:
        if occurrence.side_effect == "side_effecting":
            unique.setdefault(occurrence.action_identity, occurrence)
    return tuple(
        FaultSpec(
            case_id=tape.case_id,
            action_identity=occurrence.action_identity,
            occurrence_id=occurrence.occurrence_id,
            name=name,
        )
        for occurrence in unique.values()
        for name in (*MANDATORY_FAULTS, *optional)
    )


def run_fault_matrix(
    tape: TapeV1,
    *,
    state_root: str | Path,
    additional: list[str] | None = None,
) -> FaultMatrixResult:
    specs = expand_fault_matrix(tape, additional=additional)
    if not specs:
        return FaultMatrixResult(
            results=(),
            prerequisite_valid=False,
            prerequisite_reason="no_side_effecting_occurrence",
        )
    occurrences = {item.occurrence_id: item for item in tape.tool_occurrences}
    root = Path(state_root).resolve()
    results = []
    for spec in specs:
        safe_identity = re.sub(r"[^A-Za-z0-9_.-]", "-", spec.action_identity)
        directory = root / safe_identity / spec.name.value
        evidence = FaultEvidenceStore(directory / "evidence.sqlite")
        result = STRATEGIES[spec.name](
            spec,
            occurrences[spec.occurrence_id],
            action_path=directory / "actions.sqlite",
            evidence=evidence,
        )
        results.append(result)
    return FaultMatrixResult(results=tuple(results), prerequisite_valid=True)
