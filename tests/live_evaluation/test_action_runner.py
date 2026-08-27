from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from release.live_evaluation.action_runner import (
    EVALUATION_ACTION_MANIFEST_REF,
    EVALUATION_ACTION_MANIFEST_SHA256,
    EVALUATION_CONTROLLED_FAILURE_MANIFEST_REF,
    EvaluationActionRunner,
    EvaluationOutcomeLookupUnavailableError,
)
from release.live_evaluation.action_sink import (
    ActionSinkUnavailableError,
    EvaluationActionSink,
)
from release.live_evaluation.fault_control import EvaluationFaultState
from zeroth.contracts.graph import (
    OperationIdentity,
    SideEffectSupport,
    derive_operation_key,
)
from zeroth.platform.dispatch.operations import OperationState, SideEffectOperationStore
from zeroth.runtime.orchestration.dispatcher import (
    NodeDispatcher,
    SideEffectReconciliationExhaustedError,
)


def _identity(*, target_ref: str = EVALUATION_ACTION_MANIFEST_REF) -> OperationIdentity:
    material = {
        "run_id": "run_evaluation_1",
        "dispatch_id": "dsp_evaluation_1",
        "idempotency_key": "idem_evaluation_1",
        "attempt": 0,
        "target_ref": target_ref,
        "call_ordinal": 0,
        "support": SideEffectSupport.IDEMPOTENT,
    }
    return OperationIdentity(
        **material,
        operation_key=derive_operation_key(
            run_id=material["run_id"],
            idempotency_key=material["idempotency_key"],
            target_ref=material["target_ref"],
            call_ordinal=material["call_ordinal"],
        ),
    )


@dataclass
class _Delegate:
    result: object = field(default_factory=object)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def run(self, manifest_ref, payload, **kwargs):
        self.calls.append({"manifest_ref": manifest_ref, "payload": payload, **kwargs})
        return self.result

    def declares_side_effect(self, manifest_ref: str) -> bool | None:
        return False if manifest_ref == "unit://read-only" else None


def _runner(tmp_path: Path) -> tuple[EvaluationActionRunner, _Delegate, EvaluationActionSink]:
    delegate = _Delegate()
    sink = EvaluationActionSink(tmp_path)
    return EvaluationActionRunner(delegate=delegate, sink=sink), delegate, sink


async def test_non_evaluation_refs_delegate_unchanged(tmp_path: Path) -> None:
    runner, delegate, _ = _runner(tmp_path)
    identity = _identity(target_ref="unit://ordinary")
    payload = {"ordinary": True}
    context = {"tenant_id": "evaluation-studio-v1"}

    result = await runner.run(
        "unit://ordinary",
        payload,
        enforcement_context=context,
        operation_identity=identity,
    )

    assert result is delegate.result
    assert delegate.calls == [
        {
            "manifest_ref": "unit://ordinary",
            "payload": payload,
            "enforcement_context": context,
            "operation_identity": identity,
        }
    ]
    assert runner.declares_side_effect("unit://read-only") is False
    assert runner.declares_side_effect("unit://unknown") is None


async def test_controlled_failure_is_local_non_side_effecting_and_never_delegates(
    tmp_path: Path,
) -> None:
    runner, delegate, sink = _runner(tmp_path)

    with pytest.raises(RuntimeError, match="controlled evaluation branch failure"):
        await runner.run(
            EVALUATION_CONTROLLED_FAILURE_MANIFEST_REF,
            {"index": 3, "query": "synthetic", "sources": []},
        )

    assert runner.declares_side_effect(EVALUATION_CONTROLLED_FAILURE_MANIFEST_REF) is False
    assert delegate.calls == []
    assert sink.marker_count() == 0


async def test_evaluation_action_uses_identity_key_and_replays_identical_output(
    tmp_path: Path,
) -> None:
    runner, delegate, sink = _runner(tmp_path)
    identity = _identity()
    payload = {
        "ticket": "synthetic-case-17",
        "status": "remediated",
        "operation_key": "payload-key-must-not-win",
    }

    first = await runner.run(
        EVALUATION_ACTION_MANIFEST_REF,
        payload,
        operation_identity=identity,
    )
    duplicate = await runner.run(
        EVALUATION_ACTION_MANIFEST_REF,
        payload,
        operation_identity=identity,
    )

    assert delegate.calls == []
    assert sink.lookup(identity.operation_key) is not None
    assert sink.lookup("payload-key-must-not-win") is None
    assert duplicate.output_data == first.output_data
    assert duplicate.output_data["operation_key"] == identity.operation_key
    assert duplicate.output_data["receipt"] == first.output_data["receipt"]
    assert duplicate.audit_record["evaluation_action_duplicate"] is True
    assert duplicate.audit_record["manifest_ref_sha256"] == EVALUATION_ACTION_MANIFEST_SHA256
    assert "manifest_ref" not in duplicate.audit_record
    assert duplicate.audit_record["cost_usd"] == 0.0
    assert duplicate.audit_record["estimated_cost_usd"] == 0.0
    assert duplicate.audit_record["cost_measurement"] == "measured"
    assert sink.marker_count() == 1


async def test_evaluation_action_requires_identity_and_valid_synthetic_payload(
    tmp_path: Path,
) -> None:
    runner, delegate, sink = _runner(tmp_path)

    with pytest.raises(ValueError, match="OperationIdentity"):
        await runner.run(
            EVALUATION_ACTION_MANIFEST_REF,
            {"ticket": "synthetic-case-1", "status": "remediated"},
        )
    with pytest.raises(ValidationError):
        await runner.run(
            EVALUATION_ACTION_MANIFEST_REF,
            {"ticket": "real-customer-1", "status": "remediated"},
            operation_identity=_identity(),
        )
    with pytest.raises(ValueError, match="target_ref"):
        await runner.run(
            EVALUATION_ACTION_MANIFEST_REF,
            {"ticket": "synthetic-case-2", "status": "remediated"},
            operation_identity=_identity(target_ref="unit://different-action"),
        )

    assert delegate.calls == []
    assert sink.marker_count() == 0


async def test_outcome_lookup_returns_dispatcher_compatible_prior_output(tmp_path: Path) -> None:
    runner, _, _ = _runner(tmp_path)
    identity = _identity()
    assert await runner.outcome_lookup(identity) is None

    completed = await runner.run(
        EVALUATION_ACTION_MANIFEST_REF,
        {"ticket": "synthetic-case-2", "status": "remediated"},
        operation_identity=identity,
    )

    receipt = await runner.outcome_lookup(identity)
    assert receipt is not None
    assert json.loads(receipt) == completed.output_data


async def test_controlled_fault_hooks_pass_through_and_timeout_is_recoverable(
    tmp_path: Path,
) -> None:
    runner, _, sink = _runner(tmp_path)
    unavailable = _identity(target_ref=EVALUATION_ACTION_MANIFEST_REF)
    with pytest.raises(ActionSinkUnavailableError) as unavailable_error:
        await runner.run(
            EVALUATION_ACTION_MANIFEST_REF,
            {
                "ticket": "synthetic-unavailable",
                "status": "remediated",
                "fault": "unavailable",
            },
            operation_identity=unavailable,
        )
    assert unavailable_error.value.audit_record["cost_usd"] == 0.0
    assert unavailable_error.value.audit_record["cost_measurement"] == "measured"
    assert (
        unavailable_error.value.audit_record["manifest_ref_sha256"]
        == EVALUATION_ACTION_MANIFEST_SHA256
    )
    assert "manifest_ref" not in unavailable_error.value.audit_record
    assert sink.lookup(unavailable.operation_key) is None

    timeout = OperationIdentity(
        **(
            base := {
                "run_id": "run_evaluation_timeout",
                "dispatch_id": "dsp_evaluation_timeout",
                "idempotency_key": "idem_evaluation_timeout",
                "attempt": 0,
                "target_ref": EVALUATION_ACTION_MANIFEST_REF,
                "call_ordinal": 0,
                "support": SideEffectSupport.IDEMPOTENT,
            }
        ),
        operation_key=derive_operation_key(
            run_id=base["run_id"],
            idempotency_key=base["idempotency_key"],
            target_ref=base["target_ref"],
            call_ordinal=base["call_ordinal"],
        ),
    )
    with pytest.raises(TimeoutError, match="after commit") as timeout_error:
        await runner.run(
            EVALUATION_ACTION_MANIFEST_REF,
            {
                "ticket": "synthetic-timeout",
                "status": "remediated",
                "fault": "timeout_after_commit",
            },
            operation_identity=timeout,
        )
    assert timeout_error.value.audit_record["cost_usd"] == 0.0
    assert timeout_error.value.audit_record["cost_measurement"] == "measured"

    recovered = await runner.outcome_lookup(timeout)
    assert recovered is not None
    retry = await runner.run(
        EVALUATION_ACTION_MANIFEST_REF,
        {"ticket": "synthetic-timeout", "status": "remediated"},
        operation_identity=timeout,
    )
    assert retry.output_data == json.loads(recovered)
    assert sink.marker_count() == 1


async def test_shared_fault_control_is_consumed_by_action_runner(tmp_path: Path) -> None:
    delegate = _Delegate()
    sink = EvaluationActionSink(tmp_path / "sink")
    state = EvaluationFaultState(tmp_path / "faults.sqlite3")
    state.arm(
        campaign_id="evaluation-studio-v1",
        target="action_sink",
        mode="unavailable",
    )
    runner = EvaluationActionRunner(
        delegate=delegate,
        sink=sink,
        fault_state=state,
        campaign_id="evaluation-studio-v1",
    )

    with pytest.raises(ActionSinkUnavailableError):
        await runner.run(
            EVALUATION_ACTION_MANIFEST_REF,
            {"ticket": "synthetic-controller", "status": "remediated"},
            operation_identity=_identity(),
        )

    assert state.consume(campaign_id="evaluation-studio-v1", target="action_sink") is None
    assert sink.marker_count() == 0


async def test_campaign_lookup_fault_is_once_after_timeout_commit(tmp_path: Path) -> None:
    delegate = _Delegate()
    sink = EvaluationActionSink(tmp_path / "sink")
    state = EvaluationFaultState(tmp_path / "faults.sqlite3")
    state.arm(
        campaign_id="evaluation-studio-v1",
        target="action_outcome_lookup",
        mode="unavailable",
    )
    runner = EvaluationActionRunner(
        delegate=delegate,
        sink=sink,
        fault_state=state,
        campaign_id="evaluation-studio-v1",
    )
    identity = _identity()

    with pytest.raises(TimeoutError, match="after commit"):
        await runner.run(
            EVALUATION_ACTION_MANIFEST_REF,
            {
                "ticket": "synthetic-ambiguous-controller",
                "status": "remediated",
                "fault": "timeout_after_commit",
            },
            operation_identity=identity,
        )

    assert sink.marker_count() == 1
    with pytest.raises(EvaluationOutcomeLookupUnavailableError, match="unavailable"):
        await runner.outcome_lookup(identity)

    recovered = await runner.outcome_lookup(identity)
    assert recovered is not None
    assert json.loads(recovered)["operation_key"] == identity.operation_key
    assert sink.marker_count() == 1
    assert delegate.calls == []


async def test_timeout_lookup_outage_stays_ambiguous_until_operator_resolution(
    tmp_path: Path,
    sqlite_db,
) -> None:
    class CountingSink(EvaluationActionSink):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.execution_count = 0

        def execute(self, *args, **kwargs):
            self.execution_count += 1
            return super().execute(*args, **kwargs)

    sink = CountingSink(tmp_path / "sink")
    state = EvaluationFaultState(tmp_path / "faults.sqlite3")
    state.arm(
        campaign_id="evaluation-studio-v1",
        target="action_outcome_lookup",
        mode="unavailable",
    )
    runner = EvaluationActionRunner(
        delegate=_Delegate(),
        sink=sink,
        fault_state=state,
        campaign_id="evaluation-studio-v1",
    )
    store = SideEffectOperationStore.for_default_compatibility(sqlite_db)
    dispatcher = NodeDispatcher(
        agent_runners={},
        executable_unit_runner=runner,
        tool_executor=object(),
        operation_store=store,
        operation_outcome_lookup=runner.outcome_lookup,
    )
    identity = _identity()
    payload = {
        "ticket": "synthetic-ambiguous-runtime",
        "status": "remediated",
        "fault": "timeout_after_commit",
    }

    async def invoke():
        return await runner.run(
            EVALUATION_ACTION_MANIFEST_REF,
            payload,
            operation_identity=identity,
        )

    with pytest.raises(TimeoutError, match="after commit"):
        await dispatcher._guarded_side_effect(identity, invoke)
    with pytest.raises(SideEffectReconciliationExhaustedError, match="will not be re-executed"):
        await dispatcher._guarded_side_effect(identity, invoke)
    with pytest.raises(SideEffectReconciliationExhaustedError, match="will not be re-executed"):
        await dispatcher._guarded_side_effect(identity, invoke)

    ambiguous = await store.get(identity.operation_key)
    assert ambiguous is not None
    assert ambiguous["state"] == OperationState.AMBIGUOUS
    assert ambiguous["reconciliation_attempts"] == 1
    assert sink.execution_count == 1
    assert sink.marker_count() == 1
    assert state.consume(
        campaign_id="evaluation-studio-v1", target="action_outcome_lookup"
    ) is None

    authoritative = sink.lookup(identity.operation_key)
    assert authoritative is not None
    receipt = json.dumps(
        {
            "created_at": authoritative.created_at,
            "operation_key": authoritative.operation_key,
            "payload_hash": authoritative.payload_hash,
            "receipt": authoritative.receipt,
        },
        sort_keys=True,
    )
    assert await store.resolve_ambiguous(
        identity.operation_key,
        state=OperationState.COMPLETED,
        reason="authorized evaluation reconciliation",
        receipt=receipt,
    )
    resolved = await store.get(identity.operation_key)
    assert resolved is not None
    assert resolved["state"] == OperationState.COMPLETED
    assert json.loads(resolved["receipt"])["operation_key"] == identity.operation_key
    assert sink.execution_count == 1


def test_fixed_evaluation_manifest_is_always_declared_side_effecting(tmp_path: Path) -> None:
    runner, _, _ = _runner(tmp_path)
    assert runner.declares_side_effect(EVALUATION_ACTION_MANIFEST_REF) is True
