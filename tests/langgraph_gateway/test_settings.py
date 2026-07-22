import pytest
from pydantic import ValidationError

from zeroth.core.config.settings import LangGraphGatewaySettings, ZerothSettings


VALID_ENABLED = {
    "enabled": True,
    "upstream_url": "http://agent-server:8123",
    "upstream_audience": "agent-server:fixture",
    "deployment_ref": "external-agent",
}


def test_gateway_is_disabled_by_default():
    settings = LangGraphGatewaySettings()

    assert settings.enabled is False
    assert settings.upstream_url is None
    assert settings.upstream_audience is None
    assert settings.deployment_ref is None


def test_gateway_enabled_requires_upstream_identity():
    with pytest.raises(ValidationError):
        LangGraphGatewaySettings(enabled=True)


@pytest.mark.parametrize("field", ["upstream_url", "upstream_audience", "deployment_ref"])
def test_gateway_enabled_requires_each_upstream_identity_field(field):
    values = VALID_ENABLED | {field: None}

    with pytest.raises(ValidationError):
        LangGraphGatewaySettings(**values)


@pytest.mark.parametrize("field", ["upstream_audience", "deployment_ref"])
def test_gateway_enabled_rejects_blank_identity_values(field):
    with pytest.raises(ValidationError):
        LangGraphGatewaySettings(**(VALID_ENABLED | {field: "   "}))


@pytest.mark.parametrize(
    "url",
    [
        "agent-server:8123",
        "/agent-server",
        "ftp://agent-server/openapi.json",
        "http://:8123",
        "http://exa mple.com",
        "http://example.com:abc",
        " http://example.com",
        "http://example.com ",
        "http://user:password@example.com",
        "http://@example.com",
        "http://example.com?region=us",
        "http://example.com#fragment",
        "http://example.com:70000",
        "http://[::1",
    ],
)
def test_upstream_url_must_be_absolute_http_or_https(url):
    with pytest.raises(ValidationError):
        LangGraphGatewaySettings(**(VALID_ENABLED | {"upstream_url": url}))


@pytest.mark.parametrize(
    "url",
    [
        "http://agent-server",
        "https://agent-server:8123/base/path",
        "http://127.0.0.1:8123",
        "http://[::1]:8123/base",
    ],
)
def test_upstream_url_accepts_valid_http_authorities(url):
    settings = LangGraphGatewaySettings(**(VALID_ENABLED | {"upstream_url": url}))

    assert settings.upstream_url == url


@pytest.mark.parametrize(
    "field",
    [
        "connect_timeout_seconds",
        "read_timeout_seconds",
        "write_timeout_seconds",
        "pool_timeout_seconds",
        "context_ttl_seconds",
        "max_governed_body_bytes",
        "heartbeat_interval_seconds",
    ],
)
@pytest.mark.parametrize("value", [0, -1])
def test_positive_gateway_limits(field, value):
    with pytest.raises(ValidationError):
        LangGraphGatewaySettings(**(VALID_ENABLED | {field: value}))


@pytest.mark.parametrize("header", ["", "X Upstream Token", "Authorization:", "X-Key\nInjected"])
def test_upstream_credential_header_is_an_http_token(header):
    with pytest.raises(ValidationError):
        LangGraphGatewaySettings(upstream_credential_header=header)


@pytest.mark.parametrize("scheme", ["", "Api Key", "Bearer\r\nInjected"])
def test_upstream_credential_scheme_is_an_http_token(scheme):
    with pytest.raises(ValidationError):
        LangGraphGatewaySettings(upstream_credential_scheme=scheme)


def test_stale_threshold_exceeds_two_heartbeat_intervals():
    with pytest.raises(ValidationError):
        LangGraphGatewaySettings(heartbeat_interval_seconds=30, stale_threshold_seconds=60)


def test_unknown_routes_default_to_deny():
    settings = LangGraphGatewaySettings(**VALID_ENABLED)

    assert settings.unknown_endpoint_mode == "deny"


def test_supported_versions_are_pinned_and_immutable():
    settings = LangGraphGatewaySettings()

    assert settings.supported_langgraph_versions == ("1.2.9",)
    assert settings.supported_agent_server_versions == ("0.11.1",)


def test_gateway_settings_are_wired_into_top_level_settings():
    settings = ZerothSettings()

    assert isinstance(settings.langgraph_gateway, LangGraphGatewaySettings)
