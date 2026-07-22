from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from zeroth.core.econ.budget import BudgetCheckResult, BudgetEnforcer
from zeroth.core.langgraph_gateway.admission import admit
from zeroth.core.langgraph_gateway.models import AdmissionDecision, AdmissionRequest
from zeroth.core.policy.models import RunAdmissionResult


def _request(**updates: object) -> AdmissionRequest:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "principal_id": "user-1",
        "roles": ("operator",),
        "deployment_ref": "deployment-a",
        "assistant_id": "assistant-a",
        "operation": "runs.create",
        "input_payload": {"secret": "raw-payload"},
        "input_size_bytes": 25,
        "policy_bindings": ("policy://one",),
    }
    values.update(updates)
    return AdmissionRequest(**values)


class RecordingClassifier:
    def __init__(self, calls: list[object], classification: str = "internal") -> None:
        self.calls = calls
        self.classification = classification

    async def classify(self, payload: object) -> str:
        self.calls.append(("classifier", payload))
        return self.classification


class RecordingPolicyGuard:
    def __init__(self, calls: list[object], result: RunAdmissionResult) -> None:
        self.calls = calls
        self.result = result

    def evaluate_run_admission(self, request: AdmissionRequest) -> RunAdmissionResult:
        self.calls.append(("policy", request))
        return self.result


class RecordingBudget:
    def __init__(self, calls: list[object], result: BudgetCheckResult) -> None:
        self.calls = calls
        self.result = result

    async def check_budget_status(self, tenant_id: str) -> BudgetCheckResult:
        self.calls.append(("budget", tenant_id))
        return self.result


@pytest.mark.asyncio
async def test_admission_calls_classifier_policy_budget_in_order_and_carries_status() -> None:
    calls: list[Any] = []
    payload = {"secret": "raw-payload"}
    request = _request(input_payload=payload)
    decision = await admit(
        request,
        classifier=RecordingClassifier(calls, "restricted"),
        policy_guard=RecordingPolicyGuard(
            calls,
            RunAdmissionResult(allowed=True, policy_version="sha256:policy"),
        ),
        budget_checker=RecordingBudget(
            calls,
            BudgetCheckResult(allowed=True, spend_usd=3.0, cap_usd=10.0),
        ),
    )

    assert [call[0] for call in calls] == ["classifier", "policy", "budget"]
    assert calls[0][1] is payload
    classified_request = calls[1][1]
    assert classified_request.input_classification == "restricted"
    assert request.input_classification == "unclassified"
    assert calls[2] == ("budget", "tenant-a")
    assert decision.allowed is True
    assert decision.policy_version == "sha256:policy"
    assert decision.budget_spend_usd == 3.0
    assert decision.budget_cap_usd == 10.0
    assert decision.budget_check_degraded is False
    assert "raw-payload" not in repr(decision)
    assert "raw-payload" not in str(decision.model_dump())


@pytest.mark.asyncio
async def test_admission_policy_denial_short_circuits_budget() -> None:
    calls: list[Any] = []
    decision = await admit(
        _request(),
        classifier=RecordingClassifier(calls),
        policy_guard=RecordingPolicyGuard(
            calls,
            RunAdmissionResult(
                allowed=False,
                policy_version="sha256:denied",
                reason="zeroth.policy_denied",
            ),
        ),
        budget_checker=RecordingBudget(
            calls,
            BudgetCheckResult(allowed=True, spend_usd=0.0, cap_usd=10.0),
        ),
    )

    assert [call[0] for call in calls] == ["classifier", "policy"]
    assert decision.allowed is False
    assert decision.reason == "zeroth.policy_denied"
    assert decision.budget_spend_usd is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("budget", "expected_allowed", "expected_reason"),
    [
        (
            BudgetCheckResult(allowed=False, spend_usd=11.0, cap_usd=10.0),
            False,
            "zeroth.budget_denied",
        ),
        (
            BudgetCheckResult(
                allowed=True,
                spend_usd=0.0,
                cap_usd=None,
                degraded=True,
                failure_mode="fail_open",
            ),
            True,
            None,
        ),
        (
            BudgetCheckResult(
                allowed=False,
                spend_usd=0.0,
                cap_usd=0.0,
                degraded=True,
                failure_mode="fail_closed",
            ),
            False,
            "zeroth.budget_unavailable",
        ),
        (
            BudgetCheckResult(
                allowed=True,
                spend_usd=4.0,
                cap_usd=None,
            ),
            True,
            None,
        ),
    ],
)
async def test_admission_preserves_budget_posture(
    budget: BudgetCheckResult, expected_allowed: bool, expected_reason: str | None
) -> None:
    calls: list[Any] = []

    decision = await admit(
        _request(),
        classifier=RecordingClassifier(calls),
        policy_guard=RecordingPolicyGuard(
            calls,
            RunAdmissionResult(allowed=True, policy_version="sha256:policy"),
        ),
        budget_checker=RecordingBudget(calls, budget),
    )

    assert decision.allowed is expected_allowed
    assert decision.reason == expected_reason
    assert decision.budget_spend_usd == budget.spend_usd
    assert decision.budget_cap_usd == budget.cap_usd
    assert decision.budget_check_degraded is budget.degraded


@pytest.mark.asyncio
async def test_admission_unlimited_budget_is_strict_json_round_trip_safe() -> None:
    calls: list[Any] = []
    decision = await admit(
        _request(),
        classifier=RecordingClassifier(calls),
        policy_guard=RecordingPolicyGuard(
            calls,
            RunAdmissionResult(allowed=True, policy_version="sha256:policy"),
        ),
        budget_checker=RecordingBudget(
            calls,
            BudgetCheckResult(allowed=True, spend_usd=0.0, cap_usd=None),
        ),
    )

    assert decision.budget_cap_usd is None
    json.dumps(decision.model_dump(mode="json"), allow_nan=False)
    encoded = decision.model_dump_json()
    assert AdmissionDecision.model_validate_json(encoded) == decision


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fail_closed", "expected_allowed", "expected_reason", "expected_cap"),
    [
        (False, True, None, None),
        (True, False, "zeroth.budget_unavailable", 0.0),
    ],
)
async def test_admission_uses_configured_budget_enforcer_failure_posture(
    fail_closed: bool,
    expected_allowed: bool,
    expected_reason: str | None,
    expected_cap: float | None,
) -> None:
    def unavailable_backend(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("backend-only detail")

    calls: list[Any] = []
    enforcer = BudgetEnforcer(
        "http://regulus.test/v1",
        fail_closed=fail_closed,
        _transport=unavailable_backend,
    )

    decision = await admit(
        _request(),
        classifier=RecordingClassifier(calls),
        policy_guard=RecordingPolicyGuard(
            calls,
            RunAdmissionResult(allowed=True, policy_version="sha256:policy"),
        ),
        budget_checker=enforcer,
    )

    assert decision.allowed is expected_allowed
    assert decision.reason == expected_reason
    assert decision.budget_cap_usd == expected_cap
    assert decision.budget_check_degraded is True
    assert "backend-only detail" not in repr(decision)


class RecordingTransport:
    def __init__(self) -> None:
        self.requests: list[AdmissionRequest] = []

    async def forward(self, request: AdmissionRequest) -> None:
        self.requests.append(request)


async def _admit_then_forward(
    request: AdmissionRequest,
    *,
    transport: RecordingTransport,
    policy_guard: RecordingPolicyGuard,
    budget_checker: RecordingBudget,
    classifier: RecordingClassifier,
) -> None:
    """Test seam representing the Task-4 caller without implementing proxying."""
    decision = await admit(
        request,
        policy_guard=policy_guard,
        budget_checker=budget_checker,
        classifier=classifier,
    )
    if decision.allowed:
        await transport.forward(request)


@pytest.mark.asyncio
async def test_admission_budget_denial_stops_before_downstream_transport() -> None:
    calls: list[Any] = []
    transport = RecordingTransport()

    await _admit_then_forward(
        _request(),
        transport=transport,
        classifier=RecordingClassifier(calls),
        policy_guard=RecordingPolicyGuard(
            calls,
            RunAdmissionResult(allowed=True, policy_version="sha256:policy"),
        ),
        budget_checker=RecordingBudget(
            calls,
            BudgetCheckResult(allowed=False, spend_usd=11.0, cap_usd=10.0),
        ),
    )

    assert [call[0] for call in calls] == ["classifier", "policy", "budget"]
    assert transport.requests == []


@pytest.mark.asyncio
async def test_admission_missing_classifier_defaults_to_unclassified() -> None:
    calls: list[Any] = []

    await admit(
        _request(input_classification="trusted"),
        classifier=None,
        policy_guard=RecordingPolicyGuard(
            calls,
            RunAdmissionResult(allowed=True, policy_version="sha256:policy"),
        ),
        budget_checker=RecordingBudget(
            calls,
            BudgetCheckResult(allowed=True, spend_usd=0.0, cap_usd=10.0),
        ),
    )

    assert calls[0][0] == "policy"
    assert calls[0][1].input_classification == "unclassified"


class RaisingClassifier:
    async def classify(self, payload: object) -> str:
        raise RuntimeError("secret classifier detail")


class RaisingPolicyGuard:
    def evaluate_run_admission(self, request: AdmissionRequest) -> RunAdmissionResult:
        raise RuntimeError("secret policy detail")


class RaisingBudget:
    async def check_budget_status(self, tenant_id: str) -> BudgetCheckResult:
        raise RuntimeError("secret budget detail")


class ReturningClassifier:
    def __init__(self, result: object) -> None:
        self.result = result

    async def classify(self, payload: object) -> object:
        return self.result


class ReturningPolicyGuard:
    def __init__(self, result: object) -> None:
        self.result = result

    def evaluate_run_admission(self, request: AdmissionRequest) -> object:
        return self.result


class AsyncPolicyGuard:
    async def evaluate_run_admission(self, request: AdmissionRequest) -> RunAdmissionResult:
        return RunAdmissionResult(allowed=True, policy_version="sha256:unexpected")


class ReturningBudget:
    def __init__(self, result: object) -> None:
        self.result = result

    async def check_budget_status(self, tenant_id: str) -> object:
        return self.result


class NestedAwaitableBudget:
    async def check_budget_status(self, tenant_id: str) -> object:
        async def nested() -> BudgetCheckResult:
            return BudgetCheckResult(allowed=True, spend_usd=0.0, cap_usd=None)

        return nested()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("classifier", "policy_guard", "budget_checker", "expected_reason"),
    [
        (
            RaisingClassifier(),
            RecordingPolicyGuard(
                [], RunAdmissionResult(allowed=True, policy_version="sha256:unused")
            ),
            RecordingBudget([], BudgetCheckResult(allowed=True, spend_usd=0.0, cap_usd=1.0)),
            "zeroth.classifier_unavailable",
        ),
        (
            RecordingClassifier([]),
            RaisingPolicyGuard(),
            RecordingBudget([], BudgetCheckResult(allowed=True, spend_usd=0.0, cap_usd=1.0)),
            "zeroth.policy_unavailable",
        ),
        (
            RecordingClassifier([]),
            RecordingPolicyGuard(
                [], RunAdmissionResult(allowed=True, policy_version="sha256:policy")
            ),
            RaisingBudget(),
            "zeroth.budget_unavailable",
        ),
    ],
)
async def test_admission_dependency_exceptions_fail_safely_without_leaking(
    classifier: object,
    policy_guard: object,
    budget_checker: object,
    expected_reason: str,
) -> None:
    decision = await admit(
        _request(),
        classifier=classifier,
        policy_guard=policy_guard,
        budget_checker=budget_checker,
    )

    assert decision.allowed is False
    assert decision.reason == expected_reason
    assert "secret" not in repr(decision)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("classifier", "policy_guard", "budget_checker", "expected_reason"),
    [
        (
            ReturningClassifier(None),
            RecordingPolicyGuard(
                [], RunAdmissionResult(allowed=True, policy_version="sha256:unused")
            ),
            RecordingBudget([], BudgetCheckResult(allowed=True, spend_usd=0.0, cap_usd=1.0)),
            "zeroth.classifier_unavailable",
        ),
        (
            ReturningClassifier({"classification": "internal"}),
            RecordingPolicyGuard(
                [], RunAdmissionResult(allowed=True, policy_version="sha256:unused")
            ),
            RecordingBudget([], BudgetCheckResult(allowed=True, spend_usd=0.0, cap_usd=1.0)),
            "zeroth.classifier_unavailable",
        ),
        (
            RecordingClassifier([]),
            ReturningPolicyGuard(None),
            RecordingBudget([], BudgetCheckResult(allowed=True, spend_usd=0.0, cap_usd=1.0)),
            "zeroth.policy_unavailable",
        ),
        (
            RecordingClassifier([]),
            ReturningPolicyGuard({"allowed": "yes"}),
            RecordingBudget([], BudgetCheckResult(allowed=True, spend_usd=0.0, cap_usd=1.0)),
            "zeroth.policy_unavailable",
        ),
        (
            RecordingClassifier([]),
            AsyncPolicyGuard(),
            RecordingBudget([], BudgetCheckResult(allowed=True, spend_usd=0.0, cap_usd=1.0)),
            "zeroth.policy_unavailable",
        ),
        (
            RecordingClassifier([]),
            RecordingPolicyGuard(
                [], RunAdmissionResult(allowed=True, policy_version="sha256:policy")
            ),
            ReturningBudget(None),
            "zeroth.budget_unavailable",
        ),
        (
            RecordingClassifier([]),
            RecordingPolicyGuard(
                [], RunAdmissionResult(allowed=True, policy_version="sha256:policy")
            ),
            ReturningBudget({"allowed": "yes"}),
            "zeroth.budget_unavailable",
        ),
        (
            RecordingClassifier([]),
            RecordingPolicyGuard(
                [], RunAdmissionResult(allowed=True, policy_version="sha256:policy")
            ),
            NestedAwaitableBudget(),
            "zeroth.budget_unavailable",
        ),
    ],
)
async def test_admission_invalid_dependency_results_fail_safely(
    classifier: object,
    policy_guard: object,
    budget_checker: object,
    expected_reason: str,
) -> None:
    decision = await admit(
        _request(),
        classifier=classifier,
        policy_guard=policy_guard,
        budget_checker=budget_checker,
    )

    assert decision.allowed is False
    assert decision.reason == expected_reason
    assert "raw-payload" not in repr(decision)
