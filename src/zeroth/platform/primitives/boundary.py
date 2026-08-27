"""What an outbound sink may reach, declared once for every sink that has one.

Two configuration values in this codebase name a destination the deployment does
not control: an analytics connector's ``endpoint`` and a webhook subscription's
``target_url``. Both are author- or operator-supplied, both are handed straight to
an HTTP client, and neither had a check of any kind -- so ``http://169.254.169.254``
or ``http://127.0.0.1:6379`` was a valid destination and the request went out from
inside the deployment's own network position. A third, ``spool_path``, names a
filesystem destination with the same shape of problem.

**This module is deliberately narrow.** It applies to *outbound sinks whose
destination is arbitrary third-party infrastructure*, and to nothing else. The
memory and database connectors (Redis, Chroma, Elasticsearch, pgvector) and the
sandbox sidecar legitimately point at loopback and private addresses -- those are
this product's own documented self-hosted defaults, visible in
``platform.config.settings`` (``RedisSettings.host = "127.0.0.1"``,
``ChromaSettings.host = "localhost"``,
``ElasticsearchSettings.hosts = ["http://localhost:9200"]``,
``SandboxSettings.sidecar_url = "http://sandbox-sidecar:8001"``). A blanket
private-range refusal would break the default deployment topology while closing
nothing an attacker uses. The discriminator is ownership of the transport, not
the shape of the address: if the sink is an outbound client reaching somebody
else's service, it declares its bounds here.

Neither check is a substitute for network egress policy. Outbound HTTP callers use
``resolve_outbound_url`` immediately before sending and connect to the returned IP,
while retaining the original hostname for HTTP Host and TLS SNI. That makes the
policy decision and the socket destination the same value instead of allowing a
second DNS answer to redirect the connection.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import SplitResult, urlsplit, urlunsplit

__all__ = [
    "OutboundDestinationError",
    "ResolvedOutboundURL",
    "confine_path",
    "resolve_outbound_url",
    "validate_outbound_url",
]

# http(s) only: an outbound analytics or webhook sink speaks HTTP. Anything else
# (file://, gopher://, ftp://) is a redirection primitive, not a destination.
_ALLOWED_SCHEMES = frozenset({"http", "https"})

# Hostnames that name the local machine without being IP literals. Checked
# separately because they never reach the ipaddress branch below.
_LOCAL_HOSTNAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})


class OutboundDestinationError(ValueError):
    """An outbound destination is outside what its sink declared it may reach."""


@dataclass(frozen=True)
class ResolvedOutboundURL:
    """A destination approved and pinned to one public address for one request."""

    connect_url: str
    host_header: str
    sni_hostname: str


def _address_is_internal(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether an address names infrastructure inside the deployment's own position.

    ``is_private`` alone is not enough: link-local (``169.254.0.0/16``, which is
    where every major cloud's instance-metadata service lives) is private on IPv4
    but is worth naming, and reserved/multicast/unspecified ranges are neither
    private nor a legitimate outbound destination.
    """
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def _resolved_addresses(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve ``host`` to every address it currently names.

    Resolution failures return an empty list and are rejected by the caller. A
    destination cannot be approved unless its socket address is known and public.
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return []
    addresses = []
    for info in infos:
        try:
            addresses.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    return addresses


def _split_outbound_url(url: str, *, context: str) -> tuple[SplitResult, str | None, int | None]:
    """Parse an outbound URL and normalize parser failures into the boundary error."""
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname
        # Access is intentionally eager: urllib defers invalid/non-numeric/out-of-range
        # port failures until ``.port`` is read.
        port = parts.port
    except (ValueError, UnicodeError) as exc:
        raise OutboundDestinationError(f"{context}: destination URL is malformed") from exc
    return parts, host, port


def resolve_outbound_url(url: str, *, context: str) -> ResolvedOutboundURL:
    """Approve a public destination and return an IP-pinned request target.

    Call this before the socket is opened -- at configuration validation and at
    subscription creation -- so a refused destination is never contacted at all.

    Args:
        url: The caller-supplied destination.
        context: What is being configured, for the error message (e.g. the
            connector type or ``"webhook target_url"``). Never interpolate
            anything caller-supplied beyond the URL itself.

    Raises:
        OutboundDestinationError: The URL is malformed, uses a scheme other than
            http/https, or names a loopback, private, link-local, reserved,
            multicast, or unspecified address -- directly or by resolution -- or
            DNS resolution fails.
    """
    if not url or not url.strip():
        raise OutboundDestinationError(f"{context}: destination must not be empty")

    parts, host, port = _split_outbound_url(url, context=context)
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise OutboundDestinationError(
            f"{context}: destination scheme must be http or https, got {parts.scheme or '(none)'!r}"
        )
    if not host:
        raise OutboundDestinationError(f"{context}: destination has no host")
    if parts.username is not None or parts.password is not None:
        raise OutboundDestinationError(
            f"{context}: credentials in destination URLs are not permitted"
        )

    try:
        normalized = host.lower().rstrip(".").encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise OutboundDestinationError(f"{context}: destination hostname is malformed") from exc
    if normalized in _LOCAL_HOSTNAMES or normalized.endswith(".localhost"):
        raise OutboundDestinationError(f"{context}: destination {host!r} names the local machine")

    try:
        literal = ipaddress.ip_address(normalized)
    except ValueError:
        literal = None

    if literal is not None:
        if _address_is_internal(literal):
            raise OutboundDestinationError(
                f"{context}: destination {host!r} is an internal address"
            )
        approved_address = literal
    else:
        resolved = _resolved_addresses(normalized)
        if not resolved:
            raise OutboundDestinationError(f"{context}: destination {host!r} could not be resolved")
        if any(_address_is_internal(address) for address in resolved):
            raise OutboundDestinationError(
                f"{context}: destination {host!r} resolves to an internal address"
            )
        approved_address = resolved[0]

    address_text = str(approved_address)
    if isinstance(approved_address, ipaddress.IPv6Address):
        address_text = f"[{address_text}]"
    connect_netloc = address_text
    if port is not None:
        connect_netloc += f":{port}"

    host_header = normalized
    if ":" in normalized:
        host_header = f"[{normalized}]"
    if port is not None:
        host_header += f":{port}"
    return ResolvedOutboundURL(
        connect_url=urlunsplit(
            (parts.scheme.lower(), connect_netloc, parts.path, parts.query, parts.fragment)
        ),
        host_header=host_header,
        sni_hostname=normalized,
    )


def validate_outbound_url(url: str, *, context: str) -> None:
    """Refuse a malformed, unresolvable, or non-public outbound destination."""
    resolve_outbound_url(url, context=context)


def confine_path(path: str, *, root: Path, context: str) -> Path:
    """Resolve ``path`` under ``root`` and refuse anything that escapes it.

    Resolution happens before the containment check, so ``..`` traversal and a
    symlink pointing outside ``root`` are both caught -- checking the string
    would catch neither.

    Args:
        path: The caller-supplied destination path, absolute or relative.
        root: The directory the sink is confined to.
        context: What is being configured, for the error message.

    Returns:
        The resolved path, guaranteed to be inside ``root``.

    Raises:
        OutboundDestinationError: The resolved path is outside ``root``, or is
            ``root`` itself (a directory is not a writable destination).
    """
    if not path or not path.strip():
        raise OutboundDestinationError(f"{context}: path must not be empty")

    resolved_root = root.expanduser().resolve()
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = resolved_root / candidate
    # strict=False: the spool file usually does not exist yet, and its parent
    # may not either. Symlinks that DO exist are still resolved.
    resolved = candidate.resolve()

    # Strictly inside: the root itself is a directory, so accepting it here would
    # move the failure from config validation to an ``IsADirectoryError`` at write
    # time -- the wrong error at the wrong layer.
    if resolved_root not in resolved.parents:
        raise OutboundDestinationError(
            f"{context}: path {path!r} must resolve to a file inside the permitted root"
        )
    return resolved
