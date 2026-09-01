"""Small synchronous client for the initial Zeroth SaaS ingestion boundary."""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel
from zeroth.protocol import (
    BacktestRequest,
    DecisionScheduleRequest,
    ExecutionEvent,
    OutcomeEvent,
    VersionComparisonRequest,
)


class ZerothClient:
    """Send workflow evidence and backtest requests to Zeroth Cloud."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.zeroth.dev",
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
