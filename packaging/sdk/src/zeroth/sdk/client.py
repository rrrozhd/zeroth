"""Small synchronous client for the initial Zeroth SaaS ingestion boundary."""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel
from zeroth.protocol import (
    BacktestRequest,
    DecisionReportCreateRequest,
    DecisionReportDeliveryRequest,
    DecisionScheduleRequest,
    ExecutionEvent,
    MigrationEvidenceRefreshRequest,
    OutcomeEvent,
    ProbabilisticDecisionScheduleRequest,
    ProbabilisticMigrationRequest,
    RandomizedRolloutAssignmentRequest,
    RandomizedRolloutRequest,
    RandomizedRolloutVerifyRequest,
    VersionComparisonRequest,
)


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

    def create_randomized_rollout(self, request: RandomizedRolloutRequest) -> dict[str, Any]:
        """Create a randomized verification rollout for a retained decision."""
        return self._post("/v1/randomized-rollouts", request)

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
        response = self._http_client.get(
            f"{self._base_url}/v1/reports/{report_id}",
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
        response.raise_for_status()
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
        payload: BaseModel,
        *,
        timeout: httpx.Timeout | None = None,
    ) -> dict[str, Any]:
        request_options: dict[str, Any] = {}
        if timeout is not None:
            request_options["timeout"] = timeout
        response = self._http_client.post(
            f"{self._base_url}{path}",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json=payload.model_dump(mode="json"),
            **request_options,
        )
        response.raise_for_status()
        return response.json()

    def _get(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
    ) -> Any:
        response = self._http_client.get(
            f"{self._base_url}{path}",
            headers={"Authorization": f"Bearer {self._api_key}"},
            params=params,
        )
        response.raise_for_status()
        return response.json()
