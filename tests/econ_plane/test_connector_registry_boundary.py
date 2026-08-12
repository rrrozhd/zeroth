"""Outbound analytics connectors declare where they may send and write.

A01-34: ``HttpJsonAdapter.send`` POSTed to ``str(config["endpoint"])`` with no
scheme/host/IP check, and ``WarehouseFileAdapter.send`` appended to
``config["spool_path"]`` with no confinement. Both refusals happen in
``validate_config``, which ``send`` and ``healthcheck`` both call, so a refused
destination is never contacted or written at all.
"""

from __future__ import annotations

import pytest

from zeroth.econ.plane.connectors.registry import (
    BigQueryAdapter,
    ClickHouseAdapter,
    EnvoyAdapter,
    LangfuseAdapter,
    LaunchDarklyAdapter,
    PosthogAdapter,
    SegmentAdapter,
    SnowflakeAdapter,
    SnowplowAdapter,
    WarehouseFileAdapter,
    build_adapter_registry,
)
from zeroth.platform.primitives.boundary import OutboundDestinationError

_HTTP_ADAPTERS = [
    LangfuseAdapter,
    PosthogAdapter,
    SegmentAdapter,
    SnowplowAdapter,
    LaunchDarklyAdapter,
    EnvoyAdapter,
]
_WAREHOUSE_ADAPTERS = [
    WarehouseFileAdapter,
    ClickHouseAdapter,
    BigQueryAdapter,
    SnowflakeAdapter,
]


@pytest.mark.parametrize("adapter_class", _HTTP_ADAPTERS)
@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1:6379/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/collect",
        "https://localhost/collect",
        "file:///etc/passwd",
    ],
)
def test_http_adapters_refuse_internal_endpoints(adapter_class, endpoint: str) -> None:
    """Every HTTP-sink adapter inherits the refusal, not just the base class."""
    adapter = adapter_class()

    with pytest.raises(OutboundDestinationError):
        adapter.validate_config({"endpoint": endpoint})


@pytest.mark.parametrize("adapter_class", _HTTP_ADAPTERS)
def test_http_adapters_accept_a_public_endpoint(adapter_class) -> None:
    adapter = adapter_class()

    adapter.validate_config({"endpoint": "https://example.com/collect"})


def test_send_refuses_before_opening_a_socket(monkeypatch) -> None:
    """The AC is "refused before any socket is opened" -- assert no client is built."""
    import httpx

    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("an HTTP client was constructed for a refused endpoint")

    monkeypatch.setattr(httpx, "Client", explode)
    adapter = LangfuseAdapter()

    with pytest.raises(OutboundDestinationError):
        adapter.send("evt", {"k": "v"}, {"endpoint": "http://169.254.169.254/"})


def test_healthcheck_surfaces_the_refusal(monkeypatch) -> None:
    """``healthcheck`` routes through ``validate_config``, so it refuses too."""
    adapter = PosthogAdapter()

    with pytest.raises(OutboundDestinationError):
        adapter.healthcheck({"endpoint": "http://127.0.0.1/"})


def test_send_connects_to_the_approved_ip_but_preserves_host_and_sni(monkeypatch) -> None:
    import ipaddress
    from unittest.mock import MagicMock

    client = MagicMock()
    client.__enter__.return_value = client
    client.post.return_value.status_code = 202
    client.post.return_value.text = "ok"
    monkeypatch.setattr(
        "zeroth.platform.primitives.boundary._resolved_addresses",
        lambda _host: [ipaddress.ip_address("93.184.216.34")],
    )
    monkeypatch.setattr("httpx.Client", lambda **_kwargs: client)

    LangfuseAdapter().send("evt", {"k": "v"}, {"endpoint": "https://analytics.example/collect"})

    assert client.post.call_args.args[0] == "https://93.184.216.34/collect"
    assert client.post.call_args.kwargs["headers"]["Host"] == "analytics.example"
    assert client.post.call_args.kwargs["follow_redirects"] is False
    assert client.post.call_args.kwargs["extensions"] == {"sni_hostname": "analytics.example"}


@pytest.mark.parametrize("adapter_class", _WAREHOUSE_ADAPTERS)
def test_warehouse_adapters_refuse_a_path_outside_the_root(
    adapter_class, tmp_path, monkeypatch
) -> None:
    from zeroth.econ.plane.config import settings

    monkeypatch.setattr(settings, "connector_spool_root", str(tmp_path))
    adapter = adapter_class()

    with pytest.raises(OutboundDestinationError):
        adapter.validate_config({"spool_path": "/etc/zeroth-escape.jsonl"})


@pytest.mark.parametrize("adapter_class", _WAREHOUSE_ADAPTERS)
def test_warehouse_adapters_refuse_traversal(adapter_class, tmp_path, monkeypatch) -> None:
    from zeroth.econ.plane.config import settings

    monkeypatch.setattr(settings, "connector_spool_root", str(tmp_path / "spool"))
    adapter = adapter_class()

    with pytest.raises(OutboundDestinationError):
        adapter.validate_config({"spool_path": "../escaped.jsonl"})


def test_warehouse_send_writes_inside_the_root(tmp_path, monkeypatch) -> None:
    from zeroth.econ.plane.config import settings

    root = tmp_path / "spool"
    monkeypatch.setattr(settings, "connector_spool_root", str(root))
    adapter = ClickHouseAdapter()

    result = adapter.send("evt", {"k": "v"}, {"spool_path": "nested/events.jsonl"})

    assert result.success is True
    written = (root / "nested" / "events.jsonl").read_text()
    assert '"event_type": "evt"' in written


def test_warehouse_send_refuses_to_write_outside_the_root(tmp_path, monkeypatch) -> None:
    from zeroth.econ.plane.config import settings

    root = tmp_path / "spool"
    root.mkdir()
    monkeypatch.setattr(settings, "connector_spool_root", str(root))
    escape_target = tmp_path / "escaped.jsonl"
    adapter = SnowflakeAdapter()

    with pytest.raises(OutboundDestinationError):
        adapter.send("evt", {"k": "v"}, {"spool_path": str(escape_target)})

    assert not escape_target.exists()


def test_every_registry_adapter_still_builds() -> None:
    """The bounds are added to the adapters, not by dropping any of them."""
    registry = build_adapter_registry()

    assert set(registry) == {
        "langfuse",
        "posthog",
        "segment",
        "snowplow",
        "launchdarkly",
        "envoy",
        "kafka",
        "clickhouse",
        "bigquery",
        "snowflake",
        "prometheus",
        "otel_metrics",
    }
