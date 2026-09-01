"""HTTP behavior of the standalone SaaS client."""

from __future__ import annotations

from decimal import Decimal

import httpx

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
        http_client=httpx.Client(
            timeout=17.0,
            transport=httpx.MockTransport(respond),
        ),
    )
    client.record_execution(
        ExecutionEvent(workflow="invoice-processing", run_id="run-1", step="extract")
    )

    assert received[0].extensions["timeout"]["read"] == 17.0


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


def test_client_lists_retained_backtests() -> None:
    received: list[httpx.Request] = []
    client = _recording_client(received)

    client.list_backtests()

    assert received[0].url.path == "/v1/backtests"


def test_client_rejects_empty_credentials() -> None:
    from zeroth.sdk import ZerothClient

    try:
        ZerothClient(api_key="  ")
    except ValueError as error:
        assert str(error) == "api_key must not be empty"
    else:  # pragma: no cover - makes a missing exception explicit
        raise AssertionError("empty api key was accepted")


def test_client_closes_its_transport() -> None:
    from zeroth.sdk import ZerothClient

    http_client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    client = ZerothClient(api_key="zth_test", http_client=http_client)

    client.close()

    assert http_client.is_closed
