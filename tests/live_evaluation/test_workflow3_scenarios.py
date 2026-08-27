from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path

import httpx
import pytest

from release.live_evaluation.action_sink import EvaluationActionSink
from release.live_evaluation.campaign_execution import (
    CampaignExecutionSettings,
    build_campaign_execution,
)
from release.live_evaluation.campaign_http import BackendObservation
from release.live_evaluation.coordinator import ActionRecorder
from release.live_evaluation.evidence import EvidenceStore
from release.live_evaluation.workflow3_scenarios import (
    RemoteWorkflow3ScenarioController,
    UnsupportedWorkflow3ScenarioError,
    Workflow3NegativeEvaluator,
    Workflow3RuntimeFacts,
    Workflow3ScenarioController,
)

_ACTION_MANIFEST_SHA256 = hashlib.sha256(
    b"evaluation://synthetic-action/v1"
).hexdigest()


def _action(scenario: str):
    execution = build_campaign_execution(
        CampaignExecutionSettings(
            campaign_id="evaluation-studio-v1",
            tenant_id="tenant-a",
            model="openai/gpt-4o-mini",
            embedding_model="openai/text-embedding-3-small",
            chroma_connector_ref="eval_chroma_v1",
        )
    )
    return next(item for item in execution.actions if item.scenario == scenario)


def _audit(operation_key: str, *, status: str = "completed") -> dict[str, object]:
    return {
        "audit_id": "audit-action",
        "node_id": "apply",
        "status": status,
        "record_signature": "hmac-sha256:deadbeef",
        "execution_metadata": {
            "operation_key": operation_key,
            "manifest_ref_sha256": _ACTION_MANIFEST_SHA256,
        },
    }


def _capture_safe_audit(
    operation_key: str, *, status: str = "completed"
) -> dict[str, object]:
    return {
        "audit_id": "audit-action",
        "node_id": "apply",
        "status": status,
        "record_signature": "hmac-sha256:deadbeef",
        "execution_metadata": {
            "operation_key": operation_key,
            "manifest_ref_sha256": _ACTION_MANIFEST_SHA256,
        },
    }


def _governance_audit() -> dict[str, object]:
    return {
        "audit_id": "audit-governance",
        "node_id": "approval.resolve",
        "status": "completed",
        "record_signature": "hmac-sha256:deadbeef",
        "execution_metadata": {},
    }


def _observation(facts: Workflow3RuntimeFacts) -> BackendObservation:
    return BackendObservation(
        evidence=("events.ndjson#event-1",),
        run={"status": facts.run_status},
        audits={"records": list(facts.audit_records), "chain_verified": True},
        workflow3=facts,
    )


def _facts(scenario: str, operation_key: str) -> Workflow3RuntimeFacts:
    common = dict(
        scenario=scenario,
        operation_key=operation_key,
        marker_count_before=0,
        marker_count_after=0,
        action_execution_count=0,
        outcome_lookup_count=0,
        automatic_reexecution_count=0,
        approval_resolve_statuses=(),
        audit_records=(_governance_audit(),),
        signed_chain_verified=True,
        run_status="failed",
        operation_state="failed",
    )
    return Workflow3RuntimeFacts(**common)


@pytest.mark.parametrize(
    ("scenario", "updates"),
    [
        (
            "negative-rejection-zero-marker",
            {"approval_resolve_statuses": (200,), "approval_decisions": ("reject",)},
        ),
        (
            "negative-sla-expiry",
            {
                "approval_state_after": "resolved",
                "approval_decisions": ("reject",),
                "sla_expired": True,
            },
        ),
        (
            "negative-sink-unavailable",
            {
                "action_execution_count": 1,
                "approval_resolve_statuses": (200,),
                "audit_records": (
                    _governance_audit(),
                    _audit("op-negative-sink-unavailable", status="failed"),
                ),
            },
        ),
        (
            "negative-refresh-before-approval",
            {
                "refreshed": True,
                "approval_id_before": "approval-1",
                "approval_id_after": "approval-1",
                "approval_state_before": "pending",
                "approval_state_after": "pending",
                "approval_decisions": ("reject",),
                "approval_resolve_statuses": (200,),
            },
        ),
        (
            "negative-cancellation-after-approval",
            {"cancelled": True, "run_status": "cancelled"},
        ),
    ],
)
def test_zero_marker_negatives_require_exact_runtime_facts(
    scenario: str, updates: dict[str, object]
) -> None:
    operation_key = f"op-{scenario}"
    facts = replace(_facts(scenario, operation_key), **updates)

    result = Workflow3NegativeEvaluator().evaluate(_action(scenario), _observation(facts))

    assert result.criteria[0].status == "pass"


def test_action_attempt_matching_uses_the_capture_safe_manifest_digest() -> None:
    scenario = "negative-sink-unavailable"
    operation_key = f"op-{scenario}"
    facts = replace(
        _facts(scenario, operation_key),
        action_execution_count=1,
        approval_resolve_statuses=(200,),
        audit_records=(
            _governance_audit(),
            _capture_safe_audit(operation_key, status="failed"),
        ),
    )

    result = Workflow3NegativeEvaluator().evaluate(_action(scenario), _observation(facts))

    assert result.criteria[0].status == "pass"


def test_sla_expiry_accepts_the_real_resolved_reject_state() -> None:
    scenario = "negative-sla-expiry"
    facts = replace(
        _facts(scenario, f"op-{scenario}"),
        approval_state_after="resolved",
        approval_decisions=("reject",),
        sla_expired=True,
    )

    result = Workflow3NegativeEvaluator().evaluate(_action(scenario), _observation(facts))

    assert result.criteria[0].status == "pass"


@pytest.mark.parametrize(
    ("scenario", "updates"),
    [
        (
            "negative-duplicate-submission",
            {"approval_resolve_statuses": (200, 409)},
        ),
        (
            "negative-restart-around-receipt",
            {"restarted": True, "run_status": "succeeded"},
        ),
        (
            "negative-timeout-after-commit",
            {"outcome_lookup_count": 1, "run_status": "succeeded"},
        ),
    ],
)
def test_one_marker_negatives_require_authoritative_deduplicated_receipt(
    tmp_path: Path, scenario: str, updates: dict[str, object]
) -> None:
    operation_key = f"op-{scenario}"
    sink = EvaluationActionSink(tmp_path / "sink")
    receipt = sink.execute(operation_key, {"ticket": "synthetic-a", "status": "remediated"})
    values = {
        **updates,
        "marker_count_after": 1,
        "action_execution_count": 1,
        "operation_state": "completed",
        "approval_resolve_statuses": updates.get("approval_resolve_statuses", (200,)),
        "terminal_receipt": receipt.receipt,
        "authoritative_receipt": receipt.receipt,
        "terminal_payload_hash": receipt.payload_hash,
        "authoritative_payload_hash": receipt.payload_hash,
        "audit_records": (_governance_audit(), _audit(operation_key)),
    }
    facts = replace(
        _facts(scenario, operation_key),
        **values,
    )

    result = Workflow3NegativeEvaluator().evaluate(_action(scenario), _observation(facts))

    assert result.criteria[0].status == "pass"


def test_unresolved_ambiguous_action_has_one_authoritative_marker_and_no_terminal_receipt(
    tmp_path: Path,
) -> None:
    scenario = "negative-ambiguous-no-reexecution"
    operation_key = "operation-ambiguous"
    sink = EvaluationActionSink(tmp_path / "sink")
    receipt = sink.execute(operation_key, {"ticket": "synthetic-ambiguous", "status": "remediated"})
    facts = replace(
        _facts(scenario, operation_key),
        marker_count_after=1,
        action_execution_count=1,
        outcome_lookup_count=1,
        operation_state="ambiguous",
        authoritative_receipt=receipt.receipt,
        authoritative_payload_hash=receipt.payload_hash,
        audit_records=(_governance_audit(), _audit(operation_key, status="failed")),
    )

    result = Workflow3NegativeEvaluator().evaluate(_action(scenario), _observation(facts))

    assert result.criteria[0].status == "pass"


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("automatic_reexecution_count", 1, "reexecution"),
        ("signed_chain_verified", False, "signed"),
        ("marker_count_after", 2, "marker"),
        ("outcome_lookup_count", 2, "lookup"),
    ],
)
def test_timeout_after_commit_fails_closed_on_invariant_violation(
    tmp_path: Path, field: str, value: object, match: str
) -> None:
    scenario = "negative-timeout-after-commit"
    operation_key = "op-timeout"
    sink = EvaluationActionSink(tmp_path / "sink")
    receipt = sink.execute(operation_key, {"ticket": "synthetic-a", "status": "remediated"})
    values = {
        "marker_count_after": 1,
        "action_execution_count": 1,
        "outcome_lookup_count": 1,
        "operation_state": "completed",
        "approval_resolve_statuses": (200,),
        "run_status": "succeeded",
        "terminal_receipt": receipt.receipt,
        "authoritative_receipt": receipt.receipt,
        "terminal_payload_hash": receipt.payload_hash,
        "authoritative_payload_hash": receipt.payload_hash,
        "audit_records": (_governance_audit(), _audit(operation_key)),
        field: value,
    }
    facts = replace(
        _facts(scenario, operation_key),
        **values,
    )

    with pytest.raises(RuntimeError, match=match):
        Workflow3NegativeEvaluator().evaluate(_action(scenario), _observation(facts))


class _Controls:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def invoke(self, *, scenario: str, checkpoint: str) -> tuple[str, ...]:
        self.calls.append((scenario, checkpoint))
        return ("events.ndjson#control-event",)


def test_controller_uses_only_explicit_safe_runtime_control_checkpoints(tmp_path: Path) -> None:
    controls = _Controls()
    controller = Workflow3ScenarioController(
        sink=EvaluationActionSink(tmp_path / "sink"), runtime_controls=controls
    )
    action = _action("negative-restart-around-receipt")

    prepared = controller.prepare(action)
    evidence = controller.control(action, checkpoint="after_receipt")

    assert prepared.marker_count_before == 0
    assert controls.calls == [(action.scenario, "after_receipt")]
    assert evidence == ("events.ndjson#control-event",)


def test_controller_derives_marker_and_receipt_from_authoritative_sink(tmp_path: Path) -> None:
    sink = EvaluationActionSink(tmp_path / "sink")
    controller = Workflow3ScenarioController(sink=sink)
    action = _action("negative-timeout-after-commit")
    prepared = controller.prepare(action)
    receipt = sink.execute(
        "operation-timeout",
        {"ticket": "synthetic-timeout", "status": "remediated"},
    )
    reported = replace(
        _facts(action.scenario, "operation-timeout"),
        action_execution_count=1,
        outcome_lookup_count=1,
        operation_state="completed",
        run_status="succeeded",
        terminal_receipt=receipt.receipt,
        terminal_payload_hash=receipt.payload_hash,
        audit_records=(_governance_audit(), _audit("operation-timeout")),
    )

    finalized = controller.finalize(action, prepared=prepared, reported=reported)

    assert finalized.marker_count_after == 1
    assert finalized.authoritative_receipt == receipt.receipt
    assert finalized.authoritative_payload_hash == receipt.payload_hash


@pytest.mark.parametrize(
    ("scenario", "decisions"),
    [
        ("negative-rejection-zero-marker", ("reject",)),
        ("negative-refresh-before-approval", ("reject",)),
        ("negative-duplicate-submission", ("approve", "approve")),
        ("negative-sla-expiry", ()),
        ("negative-timeout-after-commit", ("approve",)),
    ],
)
def test_controller_exposes_exact_approval_plan(scenario: str, decisions: tuple[str, ...]) -> None:
    assert Workflow3ScenarioController.approval_decisions(_action(scenario)) == decisions


def test_controller_fails_closed_when_required_runtime_control_is_missing(tmp_path: Path) -> None:
    controller = Workflow3ScenarioController(sink=EvaluationActionSink(tmp_path / "sink"))

    with pytest.raises(UnsupportedWorkflow3ScenarioError, match="runtime control"):
        controller.prepare(_action("negative-cancellation-after-approval"))


def test_controller_refuses_runtime_mutation_without_durable_evidence(tmp_path: Path) -> None:
    class SilentControls:
        def invoke(self, *, scenario: str, checkpoint: str) -> tuple[str, ...]:
            del scenario, checkpoint
            return ()

    controller = Workflow3ScenarioController(
        sink=EvaluationActionSink(tmp_path / "sink"), runtime_controls=SilentControls()
    )
    action = _action("negative-restart-around-receipt")
    controller.prepare(action)

    with pytest.raises(RuntimeError, match="durable evidence"):
        controller.control(action, checkpoint="after_receipt")


def test_remote_controller_prepares_resolves_and_verifies_exact_run(tmp_path: Path) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/v1/scenarios/prepare":
            return httpx.Response(
                200,
                json={
                    "fixture_id": "fixture-w3",
                    "evidence": ["events.ndjson#prepared"],
                },
            )
        if request.url.path.endswith("/verify"):
            return httpx.Response(
                200,
                json={
                    "run_id": "run-w3",
                    "evidence": ["events.ndjson#verified"],
                },
            )
        raise AssertionError(request.url)

    controller = RemoteWorkflow3ScenarioController(
        sink=EvaluationActionSink(tmp_path / "sink"),
        controller_url="http://127.0.0.1:8199",
        controller_key="controller-key",
        workflow_id="evaluation-studio-v1-governed-remediation-v1",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    action = _action("negative-rejection-zero-marker")
    prepared = controller.prepare(action)
    recorder = ActionRecorder(
        EvidenceStore(tmp_path / "evidence"), step_id="w3-remote", command_sequence=1
    )

    controlled = controller.handle_paused(
        action,
        approval_id="approval-w3",
        run_id="run-w3",
        base_url="http://127.0.0.1:8104",
        request=lambda *args, **kwargs: httpx.Response(200, json={"status": "failed"}),
        recorder=recorder,
    )
    verified = controller.verify(action, run_id="run-w3")

    assert prepared.fixture_id == "fixture-w3"
    assert controller.trace(action)["approval_decisions"] == ["reject"]
    assert controlled
    assert verified == ("events.ndjson#verified",)
    assert paths == [
        "/v1/scenarios/prepare",
        "/v1/scenarios/fixture-w3/verify",
    ]


def test_remote_cancellation_waits_for_evaluation_pre_action_barrier(
    tmp_path: Path,
) -> None:
    remote_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        remote_calls.append(request.url.path)
        if request.url.path == "/v1/scenarios/prepare":
            return httpx.Response(
                200,
                json={"fixture_id": "fixture-cancel", "evidence": ["events.ndjson#p"]},
            )
        return httpx.Response(
            200,
            json={"run_id": "run-cancel", "evidence": "events.ndjson#barrier"},
        )

    controller = RemoteWorkflow3ScenarioController(
        sink=EvaluationActionSink(tmp_path / "sink"),
        controller_url="http://127.0.0.1:8199",
        controller_key="controller-key",
        workflow_id="evaluation-studio-v1-governed-remediation-v1",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    action = _action("negative-cancellation-after-approval")
    controller.prepare(action)
    recorder = ActionRecorder(
        EvidenceStore(tmp_path / "evidence"), step_id="w3-cancel", command_sequence=1
    )
    product_calls: list[str] = []

    def product_request(method: str, url: str, **kwargs: object) -> httpx.Response:
        del method, kwargs
        product_calls.append(url)
        return httpx.Response(200, json={"status": "ok"})

    controller.handle_paused(
        action,
        approval_id="approval-initial",
        run_id="run-cancel",
        base_url="http://127.0.0.1:8104",
        request=product_request,
        recorder=recorder,
    )
    assert not any(path.endswith("/cancel") for path in product_calls)

    controller.handle_paused(
        action,
        approval_id="approval-barrier",
        run_id="run-cancel",
        base_url="http://127.0.0.1:8104",
        request=product_request,
        recorder=recorder,
    )

    assert product_calls[-1].endswith("/admin/runs/run-cancel/cancel")
    assert controller.trace(action)["cancelled"] is True
    assert remote_calls[-1].endswith("/checkpoints/approval_resolved")
