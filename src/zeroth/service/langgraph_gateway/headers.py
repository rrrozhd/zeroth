"""HTTP header sanitization for transparent Agent Server forwarding."""

from __future__ import annotations

import re
from collections.abc import Iterable

import httpx

from zeroth.core.config.settings import LangGraphGatewaySettings
from zeroth.core.secrets.provider import SecretProvider, resolve_secret_async

RawHeader = tuple[bytes, bytes]

_HOP_BY_HOP_HEADERS = frozenset(
    {
        b"connection",
        b"keep-alive",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"proxy-connection",
        b"te",
        b"trailer",
        b"transfer-encoding",
        b"upgrade",
    }
)
_CLIENT_CREDENTIAL_HEADERS = frozenset({b"authorization", b"x-api-key"})
_CREDENTIAL_HEADER_PROHIBITED = _HOP_BY_HOP_HEADERS | {b"host", b"content-length"}
_HTTP_TOKEN = re.compile(rb"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")


class UpstreamCredentialUnavailableError(RuntimeError):
    """A configured upstream credential could not be resolved safely."""

    code = "zeroth.upstream_credential_unavailable"

    def __init__(self) -> None:
        super().__init__("The configured upstream credential is unavailable")


def _connection_tokens(headers: Iterable[RawHeader]) -> set[bytes]:
    tokens: set[bytes] = set()
    for name, value in headers:
        if name.lower() != b"connection":
            continue
        tokens.update(token.strip().lower() for token in value.split(b",") if token.strip())
    return tokens


def strip_hop_by_hop_headers(headers: Iterable[RawHeader]) -> list[RawHeader]:
    """Remove standard and ``Connection``-nominated hop-by-hop fields."""
    raw_headers = list(headers)
    blocked = _HOP_BY_HOP_HEADERS | _connection_tokens(raw_headers)
    return [(name, value) for name, value in raw_headers if name.lower() not in blocked]


def _credential_header_name(name: str, headers: list[RawHeader]) -> bytes:
    try:
        encoded = name.encode("ascii")
    except UnicodeEncodeError:
        raise UpstreamCredentialUnavailableError from None
    if (
        _HTTP_TOKEN.fullmatch(encoded) is None
        or encoded.lower() in _CREDENTIAL_HEADER_PROHIBITED
        or encoded.lower() in _connection_tokens(headers)
    ):
        raise UpstreamCredentialUnavailableError
    return encoded


def _credential_header_value(scheme: str, credential: str | None) -> bytes:
    if credential is None or not credential.strip():
        raise UpstreamCredentialUnavailableError
    try:
        encoded = credential.encode("ascii")
    except UnicodeEncodeError:
        raise UpstreamCredentialUnavailableError from None
    if credential != credential.strip() or any(byte < 0x20 or byte == 0x7F for byte in encoded):
        raise UpstreamCredentialUnavailableError
    return scheme.encode("ascii") + b" " + encoded


async def prepare_upstream_request_headers(
    headers: Iterable[RawHeader],
    *,
    upstream_url: httpx.URL,
    settings: LangGraphGatewaySettings,
    secret_provider: SecretProvider,
    tenant_id: str | None = None,
) -> list[RawHeader]:
    """Sanitize client headers, rebuild ``Host``, and add upstream credentials."""
    raw_headers = list(headers)
    credential_name = _credential_header_name(settings.upstream_credential_header, raw_headers)
    blocked = _CLIENT_CREDENTIAL_HEADERS | {b"host", credential_name.lower()}
    forwarded = [
        (name, value)
        for name, value in strip_hop_by_hop_headers(raw_headers)
        if name.lower() not in blocked
    ]
    forwarded.append((b"host", upstream_url.netloc))

    credential_ref = settings.upstream_credential_ref
    if credential_ref is None:
        return forwarded

    credential = await resolve_secret_async(
        secret_provider,
        credential_ref,
        tenant_id=tenant_id,
        deployment_ref=settings.deployment_ref,
    )
    value = _credential_header_value(settings.upstream_credential_scheme, credential)
    forwarded.append((credential_name, value))
    return forwarded
