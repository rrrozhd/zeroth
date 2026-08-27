from __future__ import annotations

import hashlib
from pathlib import Path

import httpx

from release.live_evaluation.campaign_execution import (
    CampaignExecutionSettings,
    build_campaign_execution,
)
from release.live_evaluation.campaign_http import (
    BackendObservation,
    HttpBackendConfig,
    HttpCampaignExecutionBackend,
    provider_acknowledgement,
)
from release.live_evaluation.coordinator import ActionRecorder, CriterionResult, StepResult
from release.live_evaluation.evidence import EvidenceStore
from release.live_evaluation.workflow3_scenarios import Workflow3RuntimeFacts

_ACTION_MANIFEST_SHA256 = hashlib.sha256(
    b"evaluation://synthetic-action/v1"
).hexdigest()


class _Publisher:
    def publish(self, **kwargs):
        raise AssertionError(kwargs)


class _Evaluator:
    def __init__(self) -> None:
        self.observation = None

    def evaluate(self, action, observation):
        self.observation = observation
        return StepResult(tuple(
            CriterionResult(item, "pass", observation.evidence)
            for item in action.criterion_ids
        ))


class _Controller:
    def __init__(self) -> None:
        self.prepared = []
        self.finalized = []

    def prepare(self, action):
        self.prepared.append(action.scenario)
        return "prepared"

    def finalize(self, action, *, prepared, reported):
        self.finalized.append((action.scenario, prepared, reported))
        return reported


def _execution():
    return build_campaign_execution(
        CampaignExecutionSettings(
            campaign_id="evaluation-studio-v1",
            tenant_id="evaluation-studio-v1",
            model="openai/gpt-4o-mini",
            embedding_model="openai/text-embedding-3-small",
            chroma_connector_ref="eval_chroma_v1",
        )
    )


def _backend(evaluator, controller):
    execution = _execution()
    return HttpCampaignExecutionBackend(
        config=HttpBackendConfig(
            console_base_url="http://localhost:8000",
            deployment_base_urls={execution.deployments.workflow3: "http://localhost:8003"},
            campaign_id=execution.settings.campaign_id,
            provider_execution_enabled=True,
            provider_acknowledgement=provider_acknowledgement(execution.settings.campaign_id),
        ),
        client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500))),
        publisher=_Publisher(),
        evaluator=evaluator,
        contracts=execution.contracts,
        tenant_id=execution.settings.tenant_id,
        workflow3_scenario_controller=controller,
    )


def _recorder(tmp_path: Path) -> ActionRecorder:
    return ActionRecorder(EvidenceStore(tmp_path / "evidence"), step_id="w3-test", command_sequence=1)


def test_execute_dispatches_workflow3_negative_through_controller(tmp_path: Path) -> None:
    evaluator = _Evaluator()
    controller = _Controller()
    backend = _backend(evaluator, controller)
    action = next(
        item for item in _execution().actions if item.scenario == "negative-timeout-after-commit"
    )
    reported = Workflow3RuntimeFacts(
        scenario=action.scenario,
        operation_key="operation-1",
        marker_count_before=0,
        marker_count_after=1,
        action_execution_count=1,
        outcome_lookup_count=1,
        automatic_reexecution_count=0,
        approval_resolve_statuses=(200,),
        audit_records=(),
        signed_chain_verified=True,
        run_status="succeeded",
        operation_state="completed",
    )
    observation = BackendObservation(evidence=("events.ndjson#one",), workflow3=reported)
    backend._run_workflow3_transport = lambda action, recorder: observation  # type: ignore[method-assign]
    recorder = _recorder(tmp_path)

    backend.execute(action, recorder)

    assert controller.prepared == [action.scenario]
    assert controller.finalized == [(action.scenario, "prepared", reported)]
    assert evaluator.observation.workflow3 is reported


def test_workflow3_dispatch_fails_closed_without_controller(tmp_path: Path) -> None:
    evaluator = _Evaluator()
    backend = _backend(evaluator, None)
    action = next(
        item for item in _execution().actions if item.scenario == "negative-rejection-zero-marker"
    )

    try:
        backend.execute(action, _recorder(tmp_path))
    except RuntimeError as exc:
        assert "workflow3 scenario controller" in str(exc)
    else:
        raise AssertionError("missing controller did not fail closed")


def test_workflow3_runtime_facts_are_derived_from_signed_audit_not_scenario_defaults() -> None:
    evaluator = _Evaluator()
    backend = _backend(evaluator, _Controller())
    action = next(
        item for item in _execution().actions if item.scenario == "negative-timeout-after-commit"
    )
    action_record = {
        "audit_id": "audit-action",
        "record_signature": "hmac-sha256:deadbeef",
        "execution_metadata": {
            "manifest_ref_sha256": _ACTION_MANIFEST_SHA256,
            "operation_key": "operation-1",
            "operation_state": "completed",
            "operation_first_execution": True,
            "operation_reconciliation_required": False,
        },
    }
    lookup_record = {
        "audit_id": "audit-lookup",
        "record_signature": "hmac-sha256:deadbeef",
        "execution_metadata": {
            "manifest_ref_sha256": _ACTION_MANIFEST_SHA256,
            "operation_key": "operation-1",
            "operation_state": "completed",
            "operation_first_execution": False,
            "operation_reconciliation_required": True,
            "operation_replay_suppressed": True,
        },
    }
    observation = BackendObservation(
        evidence=("events.ndjson#one",),
        run={
            "status": "succeeded",
            "terminal_output": {
                "operation_key": "operation-1",
                "payload_hash": "payload-hash",
                "receipt": "local-evaluation:operation-1:payload",
            },
        },
        audits={"records": [action_record, lookup_record], "chain_verified": True},
    )

    facts = backend._workflow3_runtime_facts(
        action,
        observation,
        {
            "approval_resolve_statuses": [200],
            "approval_decisions": ["approve"],
        },
    )

    assert facts.operation_key == "operation-1"
    assert facts.action_execution_count == 1
    assert facts.outcome_lookup_count == 1
    assert facts.automatic_reexecution_count == 0
    assert facts.operation_state == "completed"
    assert facts.terminal_receipt == "local-evaluation:operation-1:payload"


def test_workflow3_runtime_facts_use_the_capture_safe_manifest_digest() -> None:
    backend = _backend(_Evaluator(), _Controller())
    action = next(
        item for item in _execution().actions if item.scenario == "negative-timeout-after-commit"
    )
    records = [
        {
            "audit_id": "audit-action",
            "record_signature": "hmac-sha256:deadbeef",
            "execution_metadata": {
                "manifest_ref_sha256": _ACTION_MANIFEST_SHA256,
                "operation_key": "operation-1",
                "operation_state": "completed",
                "operation_first_execution": True,
                "operation_reconciliation_required": False,
            },
        },
        {
            "audit_id": "audit-lookup",
            "record_signature": "hmac-sha256:deadbeef",
            "execution_metadata": {
                "manifest_ref_sha256": _ACTION_MANIFEST_SHA256,
                "operation_key": "operation-1",
                "operation_state": "completed",
                "operation_first_execution": False,
                "operation_reconciliation_required": True,
                "operation_replay_suppressed": True,
            },
        },
    ]
    observation = BackendObservation(
        evidence=("events.ndjson#one",),
        run={
            "status": "succeeded",
            "terminal_output": {
                "operation_key": "operation-1",
                "payload_hash": "payload-hash",
                "receipt": "local-evaluation:operation-1:payload",
            },
        },
        audits={"records": records, "chain_verified": True},
    )

    facts = backend._workflow3_runtime_facts(action, observation, {})

    assert facts.operation_key == "operation-1"
    assert facts.action_execution_count == 1
    assert facts.outcome_lookup_count == 1


def test_workflow3_runtime_facts_reject_missing_operation_state() -> None:
    backend = _backend(_Evaluator(), _Controller())
    action = next(
        item for item in _execution().actions if item.scenario == "negative-sink-unavailable"
    )
    observation = BackendObservation(
        evidence=("events.ndjson#one",),
        run={"status": "failed"},
        audits={
            "records": [{
                "audit_id": "audit-action",
                "record_signature": "hmac-sha256:deadbeef",
                "execution_metadata": {
                    "manifest_ref_sha256": _ACTION_MANIFEST_SHA256,
                    "operation_key": "operation-1",
                },
            }],
            "chain_verified": True,
        },
    )

    try:
        backend._workflow3_runtime_facts(action, observation, {})
    except RuntimeError as exc:
        assert "operation state" in str(exc)
    else:
        raise AssertionError("missing operation state was inferred")
