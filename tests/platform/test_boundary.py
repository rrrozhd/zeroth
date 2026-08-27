"""Outbound-destination and filesystem bounds for sinks that reach third parties.

A01-34/A02-6: analytics connector endpoints, webhook target URLs, and warehouse
spool paths were all handed straight to a client with no check, so an internal
address or a path outside any root was a valid destination.
"""

from __future__ import annotations

import ipaddress
from pathlib import Path

import pytest

from zeroth.platform.primitives import (
    OutboundDestinationError,
    confine_path,
    resolve_outbound_url,
    validate_outbound_url,
)

CONTEXT = "test sink"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:6379/",
        "http://127.0.0.1/",
        "https://localhost/hook",
        "http://LOCALHOST:8080/hook",
        "http://localhost./hook",
        "http://app.localhost/hook",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/internal",
        "http://192.168.1.1/admin",
        "http://172.16.0.1/",
        "http://[::1]/hook",
        "http://[fd00::1]/hook",
        "http://0.0.0.0/",
        # Alternate encodings of 127.0.0.1. ``ipaddress`` accepts the integer and
        # hex forms, and ``getaddrinfo`` resolves the short form -- an allowlist
        # that only compared literal dotted-quad text would miss every one.
        "http://2130706433/",
        "http://0x7f.0.0.1/",
        "http://127.1/",
        # IPv4-mapped and NAT64 IPv6 wrappers around a loopback address.
        "http://[::ffff:127.0.0.1]/",
        "http://[64:ff9b::7f00:1]/",
        # Userinfo confusion: the host is what follows '@', not what precedes it.
        "http://evil.example.com@127.0.0.1/",
    ],
)
def test_internal_destinations_are_refused(url: str) -> None:
    """Every address family that names the deployment's own position is refused."""
    with pytest.raises(OutboundDestinationError):
        validate_outbound_url(url, context=CONTEXT)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "gopher://example.com/",
        "ftp://example.com/",
        "//example.com/no-scheme",
    ],
)
def test_non_http_schemes_are_refused(url: str) -> None:
    """An outbound HTTP sink speaks http(s); anything else is a redirection tool."""
    with pytest.raises(OutboundDestinationError, match="scheme"):
        validate_outbound_url(url, context=CONTEXT)


@pytest.mark.parametrize("url", ["", "   ", "http://", "https:///path"])
def test_empty_or_hostless_destinations_are_refused(url: str) -> None:
    with pytest.raises(OutboundDestinationError):
        validate_outbound_url(url, context=CONTEXT)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/hook",
        "http://example.com:8080/events",
        "https://api.segment.io/v1/track",
        "https://8.8.8.8/collect",
    ],
)
def test_public_destinations_are_permitted(url: str) -> None:
    """A real third-party sink is exactly what these call sites exist to reach."""
    validate_outbound_url(url, context=CONTEXT)


def test_destination_userinfo_is_refused_before_resolution() -> None:
    with pytest.raises(OutboundDestinationError, match="credentials"):
        validate_outbound_url(
            "https://user:password@example.com/private",
            context=CONTEXT,
        )


def test_unresolvable_host_fails_closed() -> None:
    with pytest.raises(OutboundDestinationError, match="could not be resolved"):
        validate_outbound_url("https://this-host-does-not-exist.invalid/hook", context=CONTEXT)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com:invalid/hook",
        "http://example.com:70000/hook",
        "http://[::1/hook",
    ],
)
def test_parser_failures_are_normalized(url: str) -> None:
    with pytest.raises(OutboundDestinationError):
        validate_outbound_url(url, context=CONTEXT)


def test_resolution_returns_a_pinned_url_with_original_authority(monkeypatch) -> None:
    monkeypatch.setattr(
        "zeroth.platform.primitives.boundary._resolved_addresses",
        lambda _host: [ipaddress.ip_address("93.184.216.34")],
    )

    approved = resolve_outbound_url("https://example.com:8443/hook?q=1", context=CONTEXT)

    assert approved.connect_url == "https://93.184.216.34:8443/hook?q=1"
    assert approved.host_header == "example.com:8443"
    assert approved.sni_hostname == "example.com"


def test_error_message_names_the_context(tmp_path: Path) -> None:
    """The refusal says which sink refused, so an operator can find the config."""
    with pytest.raises(OutboundDestinationError, match="langfuse"):
        validate_outbound_url("http://127.0.0.1/", context="langfuse")


def test_confine_path_accepts_a_path_under_the_root(tmp_path: Path) -> None:
    resolved = confine_path("events.jsonl", root=tmp_path, context=CONTEXT)

    assert resolved == (tmp_path / "events.jsonl").resolve()


def test_confine_path_accepts_a_nested_path_under_the_root(tmp_path: Path) -> None:
    resolved = confine_path("nested/events.jsonl", root=tmp_path, context=CONTEXT)

    assert resolved == (tmp_path / "nested" / "events.jsonl").resolve()


def test_confine_path_refuses_traversal_out_of_the_root(tmp_path: Path) -> None:
    with pytest.raises(OutboundDestinationError):
        confine_path("../escaped.jsonl", root=tmp_path, context=CONTEXT)


def test_confine_path_refuses_an_absolute_path_outside_the_root(tmp_path: Path) -> None:
    with pytest.raises(OutboundDestinationError):
        confine_path("/etc/passwd", root=tmp_path, context=CONTEXT)


def test_confine_path_refuses_a_symlink_pointing_out_of_the_root(tmp_path: Path) -> None:
    """Resolution happens before containment, so an existing symlink cannot escape."""
    root = tmp_path / "spool"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside)

    with pytest.raises(OutboundDestinationError):
        confine_path("link/events.jsonl", root=root, context=CONTEXT)


@pytest.mark.parametrize("path", ["", "   "])
def test_confine_path_refuses_an_empty_path(path: str, tmp_path: Path) -> None:
    with pytest.raises(OutboundDestinationError):
        confine_path(path, root=tmp_path, context=CONTEXT)


@pytest.mark.parametrize("path", [".", "./"])
def test_confine_path_refuses_the_root_itself(path: str, tmp_path: Path) -> None:
    """The root is a directory; accepting it defers the failure to write time."""
    with pytest.raises(OutboundDestinationError):
        confine_path(path, root=tmp_path, context=CONTEXT)
