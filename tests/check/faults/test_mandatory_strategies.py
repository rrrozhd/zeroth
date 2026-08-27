from __future__ import annotations

from zeroth.check.faults.engine import run_fault_matrix
from zeroth.check.faults.models import FaultName

from ..replay.helpers import replay_tape


def test_all_four_mandatory_faults_execute_with_one_marker(tmp_path) -> None:
    result = run_fault_matrix(replay_tape(), state_root=tmp_path)
    assert result.prerequisite_valid is True
    assert {item.spec.name for item in result.results} == {
        FaultName.DUPLICATE_DELIVERY,
        FaultName.TIMEOUT_AFTER_EFFECT,
        FaultName.CANCELLATION_AFTER_EFFECT,
        FaultName.RESTART_AFTER_RECEIPT,
    }
    assert all(item.executed for item in result.results)
    assert all(item.marker_count == 1 for item in result.results)
    assert not any(item.safety_violation for item in result.results)


def test_optional_pre_effect_error_retries_safely(tmp_path) -> None:
    result = run_fault_matrix(
        replay_tape(), state_root=tmp_path, additional=["error_before_effect"]
    )
    optional = [item for item in result.results if item.spec.name is FaultName.ERROR_BEFORE_EFFECT]
    assert len(optional) == 1
    assert optional[0].executed is True
    assert optional[0].marker_count == 1
