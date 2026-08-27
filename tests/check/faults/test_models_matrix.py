from __future__ import annotations

import pytest
from pydantic import ValidationError

from zeroth.check.faults.catalog import MANDATORY_FAULTS, validate_additional
from zeroth.check.faults.engine import expand_fault_matrix, run_fault_matrix
from zeroth.check.faults.models import FaultName, FaultSpec

from ..replay.helpers import replay_tape


def test_fault_spec_is_strict_and_independently_versioned() -> None:
    occurrence = replay_tape().tool_occurrences[0]
    spec = FaultSpec(
        case_id="7",
        action_identity=occurrence.action_identity,
        occurrence_id=occurrence.occurrence_id,
        name=FaultName.DUPLICATE_DELIVERY,
    )
    assert spec.schema_version == "fault_spec.v1"
    with pytest.raises(ValidationError):
        FaultSpec.model_validate(spec.model_dump() | {"callback": "pkg:hook"})


def test_closed_catalog_has_four_mandatory_and_one_optional() -> None:
    assert tuple(item.value for item in MANDATORY_FAULTS) == (
        "duplicate_delivery",
        "timeout_after_effect",
        "cancellation_after_effect",
        "restart_after_receipt",
    )
    assert validate_additional(["error_before_effect"]) == (FaultName.ERROR_BEFORE_EFFECT,)
    with pytest.raises(ValueError):
        validate_additional(["pkg:hook"])


def test_matrix_expands_unique_side_effecting_identities_only(tmp_path) -> None:
    tape = replay_tape()
    assert len(expand_fault_matrix(tape)) == 4
    duplicated = tape.model_copy(
        update={"tool_occurrences": [*tape.tool_occurrences, tape.tool_occurrences[0]]}
    )
    assert len(expand_fault_matrix(duplicated)) == 4
    read_only = tape.model_copy(
        update={
            "tool_occurrences": [
                tape.tool_occurrences[0].model_copy(update={"side_effect": "read_only"})
            ]
        }
    )
    result = run_fault_matrix(read_only, state_root=tmp_path)
    assert result.prerequisite_valid is False
    assert result.prerequisite_reason == "no_side_effecting_occurrence"
