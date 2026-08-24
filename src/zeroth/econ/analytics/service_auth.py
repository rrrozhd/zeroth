"""Service-to-service auth for Zeroth's calls to the bundled Regulus control plane.

econ_plane protects its data endpoints with its own JWT (HS256, signed with
``ECP_JWT_SECRET``). When the control plane is mounted in-process under
``/regulus`` it *also* sits behind Zeroth's API-key gate. Zeroth's own self-calls
-- cost-event ingest (SDK), budget checks, and the cost API -- therefore carry
**both** credentials:

- ``X-API-Key``: Zeroth's own service key, to pass Zeroth's outer gate (harmless
  in the separate-process topology, where econ_plane simply ignores it); and
- ``Authorization: Bearer <jwt>``: a freshly minted econ_plane Admin token, to
  pass econ_plane's inner auth.

The Bearer is minted **per call** with a short TTL (via a provider callable, not
a frozen string) so a long-lived Zeroth process never silently goes stale. A
missing/expired/invalid token simply yields ``401`` from econ_plane, which the
callers treat as fail-open (decision D-12): budget allows, cost reads unavailable.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

_TOKEN_TTL_SECONDS = 300

# Providers may be called with an optional tenant_id so a single deployment can
# mint a Bearer that claims the tenant being queried (one budget check = one
# tenant). Zero-arg callers (RegulusClient/InstrumentationClient) keep the
# config-default token.
HeadersProvider = Callable[..., dict[str, str]]


def mint_econ_service_token(tenant_id: str | None = None) -> str | None:
    """Mint a short-lived econ_plane Admin JWT, or ``None`` if unavailable.

    ``tenant_id`` sets the token's ``tenant_id`` claim so a self-call can
    authenticate as the tenant it is querying; when ``None`` the configured
    ``service_principal_tenant_id`` is used (the historical default). Without
    this, every non-default tenant's budget/ingest self-call 403s on
    ``require_claimed_tenant`` and silently degrades.

    Returns ``None`` (rather than raising) when the ``regulus`` extra is not
    installed or token signing fails, so callers degrade to the fail-open path.
    """
    try:
        from jose import jwt

        from zeroth.econ.plane.config import settings as ecp_settings
    except Exception:  # noqa: BLE001 - missing extra or import-time failure
        return None

    exp = datetime.now(tz=UTC) + timedelta(seconds=_TOKEN_TTL_SECONDS)
    claims = {
        "sub": ecp_settings.service_principal_subject,
        "email": ecp_settings.service_principal_email,
        "roles": [
            role.strip() for role in ecp_settings.service_principal_roles.split(",") if role.strip()
        ],
        "tenant_id": tenant_id or ecp_settings.service_principal_tenant_id,
        "workspace_id": ecp_settings.service_principal_workspace_id,
        "iss": "econ-plane",
        "exp": int(exp.timestamp()),
    }
    try:
        return jwt.encode(claims, ecp_settings.jwt_secret, algorithm=ecp_settings.jwt_algorithm)
    except Exception:  # noqa: BLE001 - signing failure -> fail-open
        return None


def make_self_auth_headers_provider(api_key: str | None) -> HeadersProvider:
    """Build a provider that yields fresh self-auth headers for each call.

    ``api_key`` is Zeroth's own service API key (used as ``X-API-Key`` to pass
    the gated ``/regulus`` mount). It may be ``None`` -- e.g. no Zeroth service
    keys are configured -- in which case only the Bearer is attached and the
    request falls to the fail-open path if the mount is gated.
    """

    def provider(tenant_id: str | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if api_key:
            headers["X-API-Key"] = api_key
        token = mint_econ_service_token(tenant_id)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    return provider
