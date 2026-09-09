"""Small synchronous client for the initial Zeroth SaaS ingestion boundary."""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel
from zeroth.protocol import (
    BacktestRequest,
    ChargeCostRevision,
    DecisionReportCreateRequest,
    DecisionReportDeliveryRequest,
    DecisionScheduleRequest,
    ExecutionEvent,
    MigrationEvidenceRefreshRequest,
    OutcomeDefinition,
    OutcomeEvent,
    ProbabilisticDecisionScheduleRequest,
    ProbabilisticMigrationRequest,
    RandomizedRolloutAssignmentRequest,
    RandomizedRolloutRequest,
    RandomizedRolloutVerifyRequest,
    VersionComparisonRequest,
)
from zeroth.sdk.errors import ZerothTransportError, _api_error_from_response


class ZerothClient:
    """Send workflow evidence and decision requests to a Zeroth service."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        timeout: float = 10.0,
        backtest_timeout: float = 120.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        normalized_key = api_key.strip()
        if not normalized_key:
            raise ValueError("api_key must not be empty")
        self._api_key = normalized_key
        self._base_url = base_url.rstrip("/")
        self._backtest_timeout = httpx.Timeout(backtest_timeout)
        self._http_client = http_client or httpx.Client(timeout=timeout)

    def record_execution(self, event: ExecutionEvent) -> dict[str, Any]:
        """Record the measured cost of one workflow step."""
        return self._post("/v1/executions", event)

    def record_outcome(self, event: OutcomeEvent) -> dict[str, Any]:
        """Attach a business outcome to a workflow run."""
        return self._post("/v1/outcomes", event)

    def create_outcome_definition(self, definition: OutcomeDefinition) -> dict[str, Any]:
        """Declare the immutable success rule for a workflow version (Admin only)."""
        return self._post("/v1/debugger/outcome-definitions", definition)

    def record_charge_cost_revision(self, revision: ChargeCostRevision) -> dict[str, Any]:
        """Append a replacement cost assertion for an existing physical charge."""
        return self._post("/v1/charge-cost-revisions", revision)

    def list_charge_cost_revisions(
        self, charge_id: str, *, limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Read the most recent source assertions, newest first (at most 1000)."""
        return self._get(
            "/v1/charge-cost-revisions", params={"charge_id": charge_id, "limit": limit},
        )

    def create_backtest(self, request: BacktestRequest) -> dict[str, Any]:
        """Submit a candidate workflow change for economic backtesting."""
        return self._post("/v1/backtests", request, timeout=self._backtest_timeout)

    def list_backtests(self) -> list[dict[str, Any]]:
        """List immutable hosted backtest results for the current project."""
        return self._get("/v1/backtests")

    def compare_versions(self, request: VersionComparisonRequest) -> dict[str, Any]:
        """Request an evidence-gated economic decision for a workflow change."""
        return self._post("/v1/decisions/compare", request)

    def create_decision_schedule(self, request: DecisionScheduleRequest) -> dict[str, Any]:
        """Create a recurring economic workflow-version comparison."""
        return self._post("/v1/decision-schedules", request)

    def list_decision_schedules(self) -> list[dict[str, Any]]:
        """List recurring economic comparisons for the current project."""
        return self._get("/v1/decision-schedules")

    def deactivate_decision_schedule(self, schedule_id: str) -> dict[str, Any]:
        """Idempotently deactivate one recurring economic comparison."""
        return self._post(f"/v1/decision-schedules/{schedule_id}/deactivate", None)

    def list_decisions(self, *, workflow: str | None = None) -> list[dict[str, Any]]:
        """List retained economic decisions, optionally for one workflow."""
        params = {"workflow": workflow} if workflow is not None else None
        return self._get("/v1/decisions", params=params)

    def create_model_migration_decision(
        self, request: ProbabilisticMigrationRequest
    ) -> dict[str, Any]:
        """Simulate and retain a risk-calibrated model migration decision."""
        return self._post(
            "/v1/decisions/model-migration",
            request,
            timeout=self._backtest_timeout,
        )

    def list_model_migration_decisions(
        self, *, workload: str | None = None
    ) -> list[dict[str, Any]]:
        """List retained probabilistic model migration decisions."""
        params = {"workload": workload} if workload is not None else None
        return self._get("/v1/decisions/model-migrations", params=params)

    def refresh_model_migration_decision(
        self, request: MigrationEvidenceRefreshRequest
    ) -> dict[str, Any]:
        """Harvest current telemetry, simulate, and retain a fresh decision."""
        return self._post(
            "/v1/decisions/model-migration/refresh",
            request,
            timeout=self._backtest_timeout,
        )

    def create_probabilistic_decision_schedule(
        self, request: ProbabilisticDecisionScheduleRequest
    ) -> dict[str, Any]:
        """Schedule fresh evidence harvesting and probabilistic reevaluation."""
        return self._post("/v1/probabilistic-decision-schedules", request)

    def deactivate_probabilistic_decision_schedule(self, schedule_id: str) -> dict[str, Any]:
        """Idempotently deactivate one probabilistic reevaluation schedule."""
        return self._post(
            f"/v1/probabilistic-decision-schedules/{schedule_id}/deactivate",
            None,
        )

    def create_randomized_rollout(self, request: RandomizedRolloutRequest) -> dict[str, Any]:
        """Create a randomized verification rollout for a retained decision."""
        return self._post("/v1/randomized-rollouts", request)

    def stop_randomized_rollout(self, rollout_id: str) -> dict[str, Any]:
        """Idempotently stop one randomized rollout."""
        return self._post(f"/v1/randomized-rollouts/{rollout_id}/stop", None)

    def assign_randomized_rollout(
        self, rollout_id: str, *, subject_id: str, cohort: str = "default"
    ) -> dict[str, Any]:
        """Get the persisted random assignment for one stable subject."""
        return self._post(
            f"/v1/randomized-rollouts/{rollout_id}/assignments",
            RandomizedRolloutAssignmentRequest(subject_id=subject_id, cohort=cohort),
        )

    def verify_randomized_rollout(
        self, rollout_id: str, request: RandomizedRolloutVerifyRequest
    ) -> dict[str, Any]:
        """Verify causal effects and append calibration observations."""
        return self._post(
            f"/v1/randomized-rollouts/{rollout_id}/verify",
            request,
            timeout=self._backtest_timeout,
        )

    def create_decision_report(self, decision_id: str) -> dict[str, Any]:
        """Create or retrieve the immutable PDF for a retained decision."""
        return self._post(f"/v1/decisions/{decision_id}/reports", DecisionReportCreateRequest())

    def download_decision_report(self, report_id: str) -> bytes:
        """Download one authenticated PDF decision artifact."""
        response = self._request("GET", f"/v1/reports/{report_id}")
        return response.content

    def deliver_decision_report(
        self, report_id: str, request: DecisionReportDeliveryRequest
    ) -> dict[str, Any]:
        """Email an exact report artifact or authenticated report link."""
        return self._post(f"/v1/reports/{report_id}/deliveries", request)

    def close(self) -> None:
        """Close the underlying HTTP transport."""
        self._http_client.close()

    def _post(
        self,
        path: str,
        payload: BaseModel | None,
        *,
        timeout: httpx.Timeout | None = None,
    ) -> dict[str, Any]:
        request_options: dict[str, Any] = {}
        if timeout is not None:
            request_options["timeout"] = timeout
        if payload is not None:
            request_options["json"] = payload.model_dump(mode="json")
        response = self._request("POST", path, **request_options)
        return response.json()

    def _get(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
    ) -> Any:
        response = self._request("GET", path, params=params)
        return response.json()

    def _request(self, method: str, path: str, **request_options: Any) -> httpx.Response:
        try:
            response = self._http_client.request(
                method,
                f"{self._base_url}{path}",
                headers={"Authorization": f"Bearer {self._api_key}"},
                **request_options,
            )
        except httpx.RequestError as error:
            raise ZerothTransportError(original_error=error) from error
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            raise _api_error_from_response(response, secrets=(self._api_key,)) from None
        return response
