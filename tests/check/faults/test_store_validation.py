from __future__ import annotations

from zeroth.check.faults.controller import validate_fault_execution
from zeroth.check.faults.models import FaultEventKind, FaultName, FaultSpec
from zeroth.check.faults.store import FaultEvidenceStore

from ..replay.helpers import replay_tape


def _spec(name: FaultName) -> FaultSpec:
    occurrence = replay_tape().tool_occurrences[0]
    return FaultSpec(
        case_id="7",
        action_identity=occurrence.action_identity,
        occurrence_id=occurrence.occurrence_id,
        name=name,
    )


def test_store_is_append_only_ordered_and_has_no_payload_columns(tmp_path) -> None:
    spec = _spec(FaultName.DUPLICATE_DELIVERY)
    first = FaultEvidenceStore(tmp_path / "events.sqlite")
    second = FaultEvidenceStore(tmp_path / "events.sqlite")
    first.append(spec, FaultEventKind.INJECTION_ARMED, process_role="a", event_id="one")
    second.append(spec, FaultEventKind.EFFECT_MARKER_WRITTEN, process_role="b", event_id="two")
    events = first.events(spec)
    assert [sequence for sequence, _ in events] == sorted(sequence for sequence, _ in events)
    assert first.marker_count(spec) == 1
    assert "amount" not in repr(events)


def test_missing_or_reversed_observation_never_counts_as_executed(tmp_path) -> None:
    spec = _spec(FaultName.TIMEOUT_AFTER_EFFECT)
    store = FaultEvidenceStore(tmp_path / "events.sqlite")
    store.append(spec, FaultEventKind.RECOVERY_REACHED, process_role="worker")
    store.append(spec, FaultEventKind.INJECTION_REACHED, process_role="worker")
    result = validate_fault_execution(spec, store)
    assert result.executed is False
    assert "invalid_event_order" in result.reason_codes
