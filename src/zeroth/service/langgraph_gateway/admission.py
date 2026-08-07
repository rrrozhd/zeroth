"""Run admission orchestration across classification, policy, and budget."""

from __future__ import annotations

import inspect
from contextlib import suppress
from typing import Protocol

from zeroth.contracts.langgraph_gateway.models import AdmissionDecision, AdmissionRequest
from zeroth.econ.analytics import BudgetCheckResult
from zeroth.governance.policy import RunAdmissionResult

_UNAVAILABLE_POLICY_VERSION = f"sha256:{'0' * 64}"


class InputClassifier(Protocol):
    """Classify an input payload without retaining it."""

    async def classify(self, payload: object) -> str: ...


class BudgetChecker(Protocol):
    """Return the configured rich budget-check posture for a tenant."""

    async def check_budget_status(self, tenant_id: str) -> BudgetCheckResult: ...


class PolicyAdmissionChecker(Protocol):
    """Evaluate run admission constraints for a classified request."""

    def evaluate_run_admission(self, request: AdmissionRequest) -> RunAdmissionResult: ...


class UnclassifiedInputClassifier:
    """Conservative classifier used until a deployment injects a real one."""

    async def classify(self, payload: object) -> str:
        del payload
        return "unclassified"


def _consume_future_state(future: object) -> None:
    """Retrieve a rejected Future-like object's terminal exception."""
    exception = getattr(future, "exception", None)
    if not callable(exception):
        return
    with suppress(BaseException):  # CancelledError inherits BaseException on supported Python.
        exception()


def _reject_awaitable(value: object) -> object:
    """Reject and dispose of unexpected coroutine/Future-like results safely."""
    if inspect.isawaitable(value):
        cancel = getattr(value, "cancel", None)
        add_done_callback = getattr(value, "add_done_callback", None)
        if callable(cancel):
            cancel()
            if callable(add_done_callback):
                add_done_callback(_consume_future_state)
        close = getattr(value, "close", None)
        if callable(close):
            close()
        raise TypeError("dependency returned an unexpected awaitable")
    return value


async def admit(
    request: AdmissionRequest,
    *,
    policy_guard: PolicyAdmissionChecker,
    budget_checker: BudgetChecker,
    classifier: InputClassifier | None = None,
) -> AdmissionDecision:
    """Classify, apply policy, then apply budget without transport side effects."""
    active_classifier = classifier or UnclassifiedInputClassifier()
    try:
        classification = await active_classifier.classify(request.input_payload)
        _reject_awaitable(classification)
        if not isinstance(classification, str):
            raise TypeError("classifier result must be a string")
        classified_request = request.with_classification(classification)
    except Exception:  # noqa: BLE001
        return AdmissionDecision(
            allowed=False,
            policy_version=_UNAVAILABLE_POLICY_VERSION,
            reason="zeroth.classifier_unavailable",
        )

    try:
        raw_policy = policy_guard.evaluate_run_admission(classified_request)
        _reject_awaitable(raw_policy)
        policy = RunAdmissionResult.model_validate(raw_policy, strict=True)
    except Exception:  # noqa: BLE001
        return AdmissionDecision(
            allowed=False,
            policy_version=_UNAVAILABLE_POLICY_VERSION,
            reason="zeroth.policy_unavailable",
        )

    if not policy.allowed:
        return AdmissionDecision(
            allowed=False,
            policy_version=policy.policy_version,
            reason=policy.reason or "zeroth.policy_denied",
        )

    try:
        raw_budget = await budget_checker.check_budget_status(request.tenant_id)
        _reject_awaitable(raw_budget)
        budget = BudgetCheckResult.model_validate(raw_budget, strict=True)
    except Exception:  # noqa: BLE001
        return AdmissionDecision(
            allowed=False,
            policy_version=policy.policy_version,
            reason="zeroth.budget_unavailable",
            budget_check_degraded=True,
        )

    reason = None
    if not budget.allowed:
        reason = (
            "zeroth.budget_unavailable"
            if budget.failure_mode == "fail_closed"
            else "zeroth.budget_denied"
        )
    return AdmissionDecision(
        allowed=budget.allowed,
        policy_version=policy.policy_version,
        reason=reason,
        budget_spend_usd=budget.spend_usd,
        budget_cap_usd=budget.cap_usd,
        budget_check_degraded=budget.degraded,
    )
