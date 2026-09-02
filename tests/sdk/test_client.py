"""HTTP behavior of the standalone SaaS client."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest


def _recording_client(received: list[httpx.Request]):
    from zeroth.sdk import ZerothClient

    def respond(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(200, json={"accepted": True, "id": "server-id"})

    return ZerothClient(
        api_key="zth_test",
        base_url="https://api.zeroth.test",
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )


def test_client_records_execution_with_bearer_auth() -> None:
    from zeroth.protocol import ExecutionEvent

    received: list[httpx.Request] = []
    client = _recording_client(received)

    response = client.record_execution(
        ExecutionEvent(
            workflow="invoice-processing",
            run_id="run-1",
            step="extract",
            cost_usd=Decimal("0.031"),
        )
    )

    assert response == {"accepted": True, "id": "server-id"}
    assert received[0].url == "https://api.zeroth.test/v1/executions"
    assert received[0].headers["authorization"] == "Bearer zth_test"
    assert received[0].read()


def test_regular_requests_keep_the_transport_timeout() -> None:
    from zeroth.protocol import ExecutionEvent
    from zeroth.sdk import ZerothClient

    received: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(200, json={"accepted": True})

    client = ZerothClient(
        api_key="zth_test",
        base_url="https://api.zeroth.test",
        http_client=httpx.Client(
            timeout=17.0,
            transport=httpx.MockTransport(respond),
        ),
    )
    client.record_execution(
        ExecutionEvent(workflow="invoice-processing", run_id="run-1", step="extract")
    )

    assert received[0].extensions["timeout"]["read"] == 17.0


def test_client_requires_an_explicit_service_endpoint() -> None:
    from zeroth.sdk import ZerothClient

    with pytest.raises(TypeError, match="base_url"):
        ZerothClient(api_key="zth_test")  # type: ignore[call-arg]


def test_client_records_outcomes_and_submits_backtests() -> None:
    from zeroth.protocol import BacktestRequest, EconomicConstraints, OutcomeEvent

    received: list[httpx.Request] = []
    client = _recording_client(received)

    client.record_outcome(
        OutcomeEvent(workflow="invoice-processing", run_id="run-1", accepted=True)
    )
    client.create_backtest(
        BacktestRequest(
            workflow="invoice-processing",
            candidate={"model": "gpt-5-mini"},
            constraints=EconomicConstraints(min_success_rate=0.97),
        )
    )

    assert [request.url.path for request in received] == ["/v1/outcomes", "/v1/backtests"]


def test_backtest_uses_its_configured_long_running_timeout() -> None:
    from zeroth.protocol import BacktestRequest, EconomicConstraints
    from zeroth.sdk import ZerothClient

    received: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(200, json={"verdict": "abstain"})

    client = ZerothClient(
        api_key="zth_test",
        base_url="https://api.zeroth.test",
        backtest_timeout=123.0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    client.create_backtest(
        BacktestRequest(
            workflow="invoice-processing",
            candidate={"model": "gpt-5-mini"},
            constraints=EconomicConstraints(min_success_rate=0.97),
        )
    )

    assert received[0].extensions["timeout"] == {
        "connect": 123.0,
        "read": 123.0,
        "write": 123.0,
        "pool": 123.0,
    }


def test_probabilistic_decision_uses_its_configured_long_running_timeout() -> None:
    from zeroth.protocol import (
        MigrationEvidence,
        MigrationObservation,
        MigrationRiskPolicy,
        ProbabilisticMigrationRequest,
    )
    from zeroth.sdk import ZerothClient

    received: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(200, json={"verdict": "abstain"})

    client = ZerothClient(
        api_key="zth_test",
        base_url="https://api.zeroth.test",
        backtest_timeout=123.0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    observation = MigrationObservation(
        case_id="case-1",
        cost_usd=Decimal("1"),
        latency_ms=100,
        accepted=True,
        source="test",
    )
    client.create_model_migration_decision(
        ProbabilisticMigrationRequest(
            evidence=MigrationEvidence(
                workload="invoice-processing",
                incumbent_model="model-a",
                candidate_model="model-b",
                incumbent=[observation],
                candidate=[observation],
                period_request_counts=[100],
            ),
            policy=MigrationRiskPolicy(),
        )
    )

    assert received[0].extensions["timeout"] == {
        "connect": 123.0,
        "read": 123.0,
        "write": 123.0,
        "pool": 123.0,
    }


def test_client_requests_an_economic_version_decision() -> None:
    from zeroth.protocol import VersionComparisonRequest

    received: list[httpx.Request] = []
    client = _recording_client(received)

    client.compare_versions(
        VersionComparisonRequest(
            workflow="invoice-processing",
            baseline_version="v6",
            candidate_version="v7",
        )
    )

    assert received[0].url.path == "/v1/decisions/compare"


def test_client_manages_recurring_decision_scans() -> None:
    from zeroth.protocol import DecisionScheduleRequest

    received: list[httpx.Request] = []
    client = _recording_client(received)

    client.create_decision_schedule(
        DecisionScheduleRequest(
            workflow="invoice-processing",
            baseline_version="v6",
            candidate_version="v7",
            interval_minutes=1440,
        )
    )
    client.list_decision_schedules()
    client.list_decisions(workflow="invoice-processing")

    assert [request.url.path for request in received] == [
        "/v1/decision-schedules",
        "/v1/decision-schedules",
        "/v1/decisions",
    ]
    assert received[-1].url.params["workflow"] == "invoice-processing"


def test_client_operates_the_fresh_probabilistic_decision_and_rollout_loop() -> None:
    from zeroth.protocol import (
        MigrationEvidenceRefreshRequest,
        MigrationEvidenceSource,
        MigrationRiskPolicy,
        ProbabilisticDecisionScheduleRequest,
        RandomizedRolloutRequest,
        RandomizedRolloutVerifyRequest,
    )

    received: list[httpx.Request] = []
    client = _recording_client(received)
    source = MigrationEvidenceSource(
        workload="invoice-processing",
        incumbent_model="model-a",
        candidate_model="model-b",
    )
    policy = MigrationRiskPolicy(require_calibrated_forecast=False)

    client.refresh_model_migration_decision(
        MigrationEvidenceRefreshRequest(evidence_source=source, policy=policy)
    )
    client.create_probabilistic_decision_schedule(
        ProbabilisticDecisionScheduleRequest(
            evidence_source=source, policy=policy, interval_minutes=1440
        )
    )
    client.create_randomized_rollout(RandomizedRolloutRequest(decision_id="pdec_123"))
    client.assign_randomized_rollout("roll_123", subject_id="customer-7", cohort="enterprise")
    client.verify_randomized_rollout(
        "roll_123", RandomizedRolloutVerifyRequest(bootstrap_samples=200)
    )

    assert [request.url.path for request in received] == [
        "/v1/decisions/model-migration/refresh",
        "/v1/probabilistic-decision-schedules",
        "/v1/randomized-rollouts",
        "/v1/randomized-rollouts/roll_123/assignments",
        "/v1/randomized-rollouts/roll_123/verify",
    ]


def test_client_creates_downloads_and_delivers_decision_reports() -> None:
    from zeroth.protocol import DecisionReportDeliveryRequest

    received: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        received.append(request)
        if request.method == "GET":
            return httpx.Response(200, content=b"%PDF-example")
        return httpx.Response(200, json={"accepted": True})

    from zeroth.sdk import ZerothClient

    client = ZerothClient(
        api_key="zth_test",
        base_url="https://api.zeroth.test",
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )

    client.create_decision_report("pdec_123")
    pdf = client.download_decision_report("rpt_123")
    client.deliver_decision_report(
        "rpt_123",
        DecisionReportDeliveryRequest(recipients=["owner@example.com"], delivery_mode="attachment"),
    )

    assert pdf == b"%PDF-example"
    assert [(request.method, request.url.path) for request in received] == [
        ("POST", "/v1/decisions/pdec_123/reports"),
        ("GET", "/v1/reports/rpt_123"),
        ("POST", "/v1/reports/rpt_123/deliveries"),
    ]


def test_client_lists_retained_backtests() -> None:
    received: list[httpx.Request] = []
    client = _recording_client(received)

    client.list_backtests()

    assert received[0].url.path == "/v1/backtests"


def test_client_rejects_empty_credentials() -> None:
    from zeroth.sdk import ZerothClient

    try:
        ZerothClient(api_key="  ", base_url="https://api.zeroth.test")
    except ValueError as error:
        assert str(error) == "api_key must not be empty"
    else:  # pragma: no cover - makes a missing exception explicit
        raise AssertionError("empty api key was accepted")


def test_client_closes_its_transport() -> None:
    from zeroth.sdk import ZerothClient

    http_client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    client = ZerothClient(
        api_key="zth_test",
        base_url="https://api.zeroth.test",
        http_client=http_client,
    )

    client.close()

    assert http_client.is_closed
