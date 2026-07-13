"""Pre-execution budget enforcement against Regulus backend.

BudgetEnforcer checks tenant spend against budget caps via the Regulus
backend before any LLM call, using a TTL cache to avoid per-call HTTP
round trips (per D-10, D-11). Fails open when Regulus is unreachable
(per D-12), unless configured fail-closed.

The enforcer reaches the control plane either over external HTTP
(separate-process Regulus, via ``regulus_base_url``) or IN-PROCESS against
the bundled ``/regulus`` mount when an ``asgi_app`` is supplied — the
latter avoids a loopback socket and the wrong default ``base_url``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import httpx
from cachetools import TTLCache

logger = logging.getLogger(__name__)


class BudgetEnforcer:
    """Pre-execution budget check against Regulus backend (per D-10).

    Queries the Regulus ``/budget/status`` endpoint for the tenant's
    current spend and budget cap. Results are cached with a TTL to
    avoid a network round-trip on every agent call (per D-11).

    If the Regulus backend is unreachable or returns an error, the
    enforcer **allows** execution by default (fail-open, per D-12) --
    budget enforcement must never block production workloads because of
    an observability service outage. Set ``fail_closed=True`` to instead
    DENY on backend errors (tighter governance, lower availability); the
    fail-closed switch applies ONLY to the error path — a successfully
    fetched null cap always stays unlimited.

    When ``asgi_app`` is supplied the enforcer talks to that ASGI app
    in-process (the bundled ``/regulus`` mount), so a default bundled
    deploy reaches the plane on Zeroth's own port instead of the external
    ``localhost:8000`` default. When ``asgi_app`` is ``None`` the existing
    external-HTTP path is used unchanged.
    """

    def __init__(
        self,
        regulus_base_url: str | None = None,
        *,
        cache_ttl: int = 30,
        timeout: float = 5.0,
        headers_provider: Callable[[], dict[str, str]] | None = None,
        fail_closed: bool = False,
        asgi_app: Any | None = None,
        _transport: Any = None,
    ) -> None:
        # In-process mount: the bundled econ_plane sub-app serves its routes
        # under ``/v1`` (the parent strips the ``/regulus`` prefix), so the
        # internal base_url targets ``/v1/budget/status`` directly. No loopback
        # socket, no reliance on the external localhost:8000 default.
        if regulus_base_url is None:
            regulus_base_url = (
                "http://regulus.internal/v1" if asgi_app is not None else "http://localhost:8000/v1"
            )
        self._base_url = regulus_base_url.rstrip("/")
        self._timeout = timeout
        self._cache: TTLCache[str, dict[str, float | bool]] = TTLCache(maxsize=1024, ttl=cache_ttl)
        self._headers_provider = headers_provider
        self._fail_closed = fail_closed
        self._asgi_app = asgi_app
        self._transport = _transport

    async def check_budget(self, tenant_id: str) -> tuple[bool, float, float]:
        """Check whether *tenant_id* is within its budget cap.

        Returns ``(allowed, current_spend, budget_cap)``.

        * ``allowed`` is ``True`` when the tenant may proceed.
        * On a backend error the method returns ``(True, 0.0, inf)`` (fail-open,
          the default) or ``(False, 0.0, 0.0)`` (deny) when ``fail_closed=True``.
          The error path never writes the cache.
        * Successful results are cached per tenant for the configured TTL.
        """
        cached = self._cache.get(tenant_id)
        if cached is not None:
            return cached["allowed"], cached["spend"], cached["cap"]  # type: ignore[return-value]

        try:
            client_kwargs: dict[str, Any] = {"timeout": self._timeout}
            if self._asgi_app is not None:
                # In-process: dispatch straight to the mounted control plane.
                client_kwargs["transport"] = httpx.ASGITransport(app=self._asgi_app)
            elif self._transport is not None:
                client_kwargs["transport"] = httpx.MockTransport(self._transport)

            headers = self._headers_provider() if self._headers_provider is not None else None
            async with httpx.AsyncClient(**client_kwargs) as client:
                resp = await client.get(
                    f"{self._base_url}/budget/status",
                    params={"tenant_id": tenant_id},
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()
                spend = float(data.get("total_cost_usd", 0))
                # No configured cap comes back as null — unlimited, not an error.
                cap_raw = data.get("budget_cap_usd")
                cap = float(cap_raw) if cap_raw is not None else float("inf")
                allowed = spend < cap
                self._cache[tenant_id] = {"allowed": allowed, "spend": spend, "cap": cap}
                return allowed, spend, cap
        except Exception as exc:  # noqa: BLE001
            # Error path only — NEVER write self._cache here, so a transient blip
            # can't lock (or allow) a tenant for the whole TTL; the next call
            # re-probes the backend.
            if self._fail_closed:
                # Fail-closed: DENY when the control plane can't be reached.
                logger.warning(
                    "budget check failing CLOSED (deny) for tenant %s: %s (%s)",
                    tenant_id,
                    exc.__class__.__name__,
                    exc,
                )
                return False, 0.0, 0.0
            # Fail-open (default): Regulus unavailability must not block execution
            # (D-12). But a silent fail-open means budget governance can evaporate
            # with no trace — emit a warning so operators can see caps aren't being
            # enforced and alert on it.
            logger.warning(
                "budget check failed open for tenant %s: %s (%s) — spend cap NOT enforced",
                tenant_id,
                exc.__class__.__name__,
                exc,
            )
            return True, 0.0, float("inf")
