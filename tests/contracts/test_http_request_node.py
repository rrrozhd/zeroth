from __future__ import annotations

import pytest
from pydantic import ValidationError

from zeroth.contracts.graph import HttpRequestNode, HttpRequestNodeData


def _data(**overrides: object) -> HttpRequestNodeData:
    values: dict[str, object] = {"url": "http://127.0.0.1:8787/scenario"}
    values.update(overrides)
    return HttpRequestNodeData(**values)  # type: ignore[arg-type]


def test_http_request_node_is_get_only_and_governed() -> None:
    node = HttpRequestNode(
        node_id="probe",
        graph_version_ref="http-demo:v1",
        http_request=_data(),
    )

    assert node.node_type == "http_request"
    assert node.http_request.method == "GET"
    spec = node.to_governed_step_spec()
    assert spec.tool["kind"] == "http_request_ref"
    assert spec.tool["capability_refs"] == ["network_read", "external_api_call"]


def test_http_request_node_cannot_clear_its_required_capabilities() -> None:
    with pytest.raises(ValidationError):
        HttpRequestNode(
            node_id="probe",
            graph_version_ref="http-demo:v1",
            capability_bindings=[],
            http_request=_data(),
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://api.example.com/data",
        "http://8.8.8.8/data",
        "ftp://127.0.0.1/data",
        "http://user:password@127.0.0.1/data",
        "http://127.0.0.1/data?token=secret",
        "http://127.0.0.1/data#fragment",
        "http://0.0.0.0/data",
        "http://169.254.169.254/latest/meta-data",
        "http://[fe80::1]/data",
        "http://127.0.0.1/data\nHost:example.com",
    ],
)
def test_http_request_node_rejects_unsafe_destinations(url: str) -> None:
    with pytest.raises(ValidationError):
        _data(url=url)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8787/data",
        "http://127.0.0.1:8787/data",
        "http://10.0.0.8/data",
        "https://192.168.1.10/data",
        "http://[::1]:8787/data",
        "http://[fd00::1]/data",
    ],
)
def test_http_request_node_accepts_literal_loopback_and_private_destinations(url: str) -> None:
    assert _data(url=url).url == url


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timeout_seconds", 0),
        ("timeout_seconds", 31),
        ("max_retries", -1),
        ("max_retries", 6),
        ("max_response_bytes", 0),
        ("max_response_bytes", 1_048_577),
        ("retryable_status_codes", set()),
        ("retryable_status_codes", {200}),
        ("retryable_status_codes", {600}),
    ],
)
def test_http_request_node_rejects_out_of_range_controls(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        _data(**{field: value})
