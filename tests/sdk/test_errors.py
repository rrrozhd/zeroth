"""Typed and sanitized failure behavior of the standalone SaaS client."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

API_KEY = "zth_super_secret_credential"
BASE_URL = "https://api.zeroth.test"


def _client(response_factory: Callable[[httpx.Request], httpx.Response]):
    from zeroth.sdk import ZerothClient

    return ZerothClient(
        api_key=API_KEY,
        base_url=BASE_URL,
        http_client=httpx.Client(transport=httpx.MockTransport(response_factory)),
    )


@pytest.mark.parametrize(
    ("status_code", "error_name"),
    [
        (401, "ZerothAuthenticationError"),
        (402, "ZerothEntitlementError"),
        (403, "ZerothAuthorizationError"),
        (404, "ZerothNotFoundError"),
        (409, "ZerothConflictError"),
        (422, "ZerothValidationError"),
        (429, "ZerothRateLimitError"),
        (500, "ZerothServerError"),
        (599, "ZerothServerError"),
    ],
)
def test_statuses_map_to_public_typed_api_errors(status_code: int, error_name: str) -> None:
    import zeroth.sdk as sdk

    captured: list[httpx.Response] = []

    def respond(request: httpx.Request) -> httpx.Response:
        response = httpx.Response(status_code, json={"detail": "bounded failure"})
        captured.append(response)
        return response

    client = _client(respond)
    error_type = getattr(sdk, error_name)

    with pytest.raises(error_type) as raised:
        client.list_backtests()

    error = raised.value
    assert isinstance(error, sdk.ZerothAPIError)
    assert isinstance(error, sdk.ZerothSDKError)
    assert isinstance(error, httpx.HTTPStatusError)
    assert error.status_code == status_code
    assert error.detail == "bounded failure"
    assert error.response is captured[0]
    assert error.request is captured[0].request


def test_unclassified_http_failure_uses_the_common_api_error() -> None:
    from zeroth.sdk import ZerothAPIError

    client = _client(lambda _: httpx.Response(418, json={"detail": "teapot"}))

    with pytest.raises(ZerothAPIError) as raised:
        client.list_backtests()

    assert type(raised.value) is ZerothAPIError
    assert raised.value.status_code == 418


def test_transport_failure_preserves_httpx_compatibility_request_and_cause() -> None:
    from zeroth.sdk import ZerothSDKError, ZerothTransportError

    captured: list[httpx.ConnectError] = []

    def fail(request: httpx.Request) -> httpx.Response:
        error = httpx.ConnectError(f"connection failed for Bearer {API_KEY}", request=request)
        captured.append(error)
        raise error

    client = _client(fail)

    with pytest.raises(httpx.RequestError) as legacy_catch:
        client.list_backtests()

    error = legacy_catch.value
    assert isinstance(error, ZerothTransportError)
    assert isinstance(error, ZerothSDKError)
    assert error.request is captured[0].request
    assert error.original_error is captured[0]
    assert error.__cause__ is captured[0]
    assert API_KEY not in str(error)


def test_transport_failure_without_an_attached_request_maps_safely() -> None:
    from zeroth.sdk import ZerothTransportError

    original = httpx.ConnectError("connection failed before request construction")

    def fail(_: httpx.Request) -> httpx.Response:
        raise original

    with pytest.raises(ZerothTransportError) as raised:
        _client(fail).list_backtests()

    assert raised.value.original_error is original
    assert raised.value.__cause__ is original


def test_api_error_extracts_bounded_request_and_correlation_ids() -> None:
    from zeroth.sdk import ZerothNotFoundError

    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            headers={"X-Request-ID": "req-123", "X-Correlation-ID": "corr-456"},
            json={"detail": "missing"},
        )

    with pytest.raises(ZerothNotFoundError) as raised:
        _client(respond).list_backtests()

    assert raised.value.request_id == "req-123"
    assert raised.value.correlation_id == "corr-456"
    assert "request_id=req-123" in str(raised.value)
    assert "correlation_id=corr-456" in str(raised.value)


def test_api_error_extracts_ids_from_json_when_headers_are_absent() -> None:
    from zeroth.sdk import ZerothConflictError

    with pytest.raises(ZerothConflictError) as raised:
        _client(
            lambda _: httpx.Response(
                409,
                json={
                    "detail": "conflict",
                    "request_id": "body-request",
                    "correlation_id": "body-correlation",
                },
            )
        ).list_backtests()

    assert raised.value.request_id == "body-request"
    assert raised.value.correlation_id == "body-correlation"


def test_api_error_bounds_structured_detail_and_redacts_the_client_credential() -> None:
    from zeroth.sdk import ZerothValidationError

    hostile = f"credential={API_KEY};" + "x" * 4_000
    with pytest.raises(ZerothValidationError) as raised:
        _client(lambda _: httpx.Response(422, json={"detail": [{"msg": hostile}]})).list_backtests()

    assert len(raised.value.detail) <= 512
    assert API_KEY not in raised.value.detail
    assert API_KEY not in str(raised.value)
    assert "[REDACTED]" in raised.value.detail
    assert str(raised.value).endswith("…")


@pytest.mark.parametrize(
    ("content", "content_type"),
    [
        (b"not-json " + b"hostile" * 1_000, "text/plain"),
        (b'{"detail":', "application/json"),
    ],
)
def test_malformed_or_non_json_failure_body_is_not_rendered(
    content: bytes, content_type: str
) -> None:
    from zeroth.sdk import ZerothServerError

    with pytest.raises(ZerothServerError) as raised:
        _client(
            lambda _: httpx.Response(500, content=content, headers={"Content-Type": content_type})
        ).list_backtests()

    assert raised.value.detail == "Internal Server Error"
    assert "hostile" not in str(raised.value)
    assert len(str(raised.value)) < 800


def test_report_download_uses_the_same_checked_error_boundary() -> None:
    from zeroth.sdk import ZerothEntitlementError

    with pytest.raises(ZerothEntitlementError):
        _client(
            lambda _: httpx.Response(402, json={"detail": "upgrade required"})
        ).download_decision_report("rpt_123")
