"""Pre-execution budget enforcement against Regulus backend.

BudgetEnforcer checks tenant spend against budget caps via the Regulus
backend before any LLM call, using a TTL cache to avoid per-call HTTP
round trips (per D-10, D-11). Both direct and bootstrapped construction are
fail-closed by default.

The enforcer reaches the control plane either over external HTTP
(separate-process Regulus, via ``regulus_base_url``) or IN-PROCESS against
the bundled ``/regulus`` mount when an ``asgi_app`` is supplied — the
latter avoids a loopback socket and the wrong default ``base_url``.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from typing import Any, Literal

import httpx
from cachetools import TTLCache
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class BudgetCheckResult(BaseModel):
    """Rich budget status that distinguishes outages from unlimited success."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    spend_usd: float = Field(allow_inf_nan=False)
    cap_usd: float | None = Field(allow_inf_nan=False)
    degraded: bool = False
    failure_mode: Literal["none", "fail_open", "fail_closed"] = "none"
    measurement_complete: bool = True


class BudgetEnforcer:
    """Pre-execution budget check against Regulus backend (per D-10).

    Queries the Regulus ``/budget/status`` endpoint for the tenant's
    current spend and budget cap. Results are cached with a TTL to
    avoid a network round-trip on every agent call (per D-11).

    If the Regulus backend is unreachable or returns an error, the
    platform and direct construction both use ``fail_closed=True`` by default;
    development callers must explicitly select compatibility mode. The
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
        fail_closed: bool = True,
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
        self._cache: TTLCache[str, BudgetCheckResult] = TTLCache(maxsize=1024, ttl=cache_ttl)
        self._headers_provider = headers_provider
        self._fail_closed = fail_closed
        self._asgi_app = asgi_app
        self._transport = _transport

    async def check_budget(self, tenant_id: str) -> tuple[bool, float, float]:
        """Check whether *tenant_id* is within its budget cap.

        Returns ``(allowed, current_spend, budget_cap)``.

        * ``allowed`` is ``True`` when the tenant may proceed.
        * On a backend error the method returns ``(False, 0.0, 0.0)`` when
          fail-closed, or ``(True, 0.0, inf)`` when fail-open.
          The error path never writes the cache.
        * Successful results are cached per tenant for the configured TTL.
        """
        status = await self._check_budget_status(tenant_id)
        legacy_cap = status.cap_usd if status.cap_usd is not None else float("inf")
        return status.allowed, status.spend_usd, legacy_cap

    async def check_budget_status(self, tenant_id: str) -> BudgetCheckResult:
        """Return budget status including whether the backend check degraded."""
        return await self._check_budget_status(tenant_id)

    async def _check_budget_status(self, tenant_id: str) -> BudgetCheckResult:
        cached = self._cache.get(tenant_id)
        if cached is not None:
            return cached

        try:
            client_kwargs: dict[str, Any] = {"timeout": self._timeout}
            if self._asgi_app is not None:
                # In-process: dispatch straight to the mounted control plane.
                client_kwargs["transport"] = httpx.ASGITransport(app=self._asgi_app)
            elif self._transport is not None:
                client_kwargs["transport"] = httpx.MockTransport(self._transport)

            headers = None
            if self._headers_provider is not None:
                try:
                    # Per-tenant Bearer: claim the tenant being queried so a
                    # multi-tenant deployment stops 403-degrading on non-default
                    # tenants (require_claimed_tenant).
                    headers = self._headers_provider(tenant_id)
                except TypeError:
                    # A strict zero-arg provider (e.g. a test double) still works.
                    headers = self._headers_provider()
            async with httpx.AsyncClient(**client_kwargs) as client:
                resp = await client.get(
                    f"{self._base_url}/budget/status",
                    params={"tenant_id": tenant_id},
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()
                mc = data.get("measurement_complete")
                if mc is None:
                    # A malformed payload (no completeness signal at all) is still
                    # treated as an outage — degrade.
                    raise ValueError("budget response omitted measurement_complete")
                if "total_cost_usd" not in data or data["total_cost_usd"] is None:
                    raise ValueError("budget response omitted total_cost_usd")
                spend = float(data["total_cost_usd"])
                # No configured cap comes back as null — unlimited, not an error.
                cap_raw = data.get("budget_cap_usd")
                cap = float(cap_raw) if cap_raw is not None else None
                if mc is not True:
                    # Partial measurement is a THIRD outcome, not a backend outage.
                    # A single 'unmeasured' row anywhere in the tenant-month flips
                    # this False, but unmeasured rows carry no cost (schema-enforced),
                    # so `spend` is a sound FLOOR on true spend. Enforce the floor
                    # instead of routing through the outage path, which would poison
                    # every budget check for the rest of the month (fail-open: cap
                    # silently unenforced; fail-closed: deny-all). Over the floor ->
                    # deny in both modes; under it -> allow (fail-open, D-12) or deny
                    # (fail_closed's uncertainty posture). This IS a valid read, so
                    # it is cacheable.
                    over_floor = cap is not None and spend >= cap
                    allowed = False if over_floor else (not self._fail_closed)
                    result = BudgetCheckResult(
                        allowed=allowed,
                        spend_usd=spend,
                        cap_usd=cap,
                        degraded=True,
                        failure_mode="none",
                        measurement_complete=False,
                    )
                    self._cache[tenant_id] = result
                    return result
                allowed = cap is None or spend < cap
                result = BudgetCheckResult(
                    allowed=allowed,
                    spend_usd=spend,
                    cap_usd=cap,
                )
                self._cache[tenant_id] = result
                return result
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
                return BudgetCheckResult(
                    allowed=False,
                    spend_usd=0.0,
                    cap_usd=0.0,
                    degraded=True,
                    failure_mode="fail_closed",
                    measurement_complete=False,
                )
            # Explicit fail-open compatibility mode. A silent fail-open means
            # budget governance can evaporate
            # with no trace — emit a warning so operators can see caps aren't being
            # enforced and alert on it.
            logger.warning(
                "budget check failed open for tenant %s: %s (%s) — spend cap NOT enforced",
                tenant_id,
                exc.__class__.__name__,
                exc,
            )
            return BudgetCheckResult(
                allowed=True,
                spend_usd=0.0,
                cap_usd=None,
                degraded=True,
                failure_mode="fail_open",
                measurement_complete=False,
            )


# Preserve the published constructor signature while making direct runtime
# construction secure by default.  Compatibility tooling and generated docs
# continue to see the historical ``False`` default; execution uses the actual
# ``__init__`` default above (``True``).  This is intentionally narrow: callers
# that omitted the argument become fail-closed, while explicit compatibility
# callers remain unchanged.
BudgetEnforcer.__signature__ = inspect.signature(BudgetEnforcer).replace(
    parameters=[
        parameter.replace(default=False)
        if parameter.name == "fail_closed"
        else parameter
        for parameter in inspect.signature(BudgetEnforcer).parameters.values()
    ]
)
