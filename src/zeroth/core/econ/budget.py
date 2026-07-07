"""Pre-execution budget enforcement against Regulus backend.

BudgetEnforcer checks tenant spend against budget caps via the Regulus
backend before any LLM call, using a TTL cache to avoid per-call HTTP
round trips (per D-10, D-11). Fails open when Regulus is unreachable
(per D-12).
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

    Queries the Regulus ``/dashboard/kpis`` endpoint for the tenant's
    current spend and budget cap. Results are cached with a TTL to
    avoid a network round-trip on every agent call (per D-11).

    If the Regulus backend is unreachable or returns an error, the
    enforcer **allows** execution (fail-open, per D-12) -- budget
    enforcement must never block production workloads because of an
    observability service outage.
    """

    def __init__(
        self,
        regulus_base_url: str,
        *,
        cache_ttl: int = 30,
        timeout: float = 5.0,
        headers_provider: Callable[[], dict[str, str]] | None = None,
        _transport: Any = None,
    ) -> None:
        self._base_url = regulus_base_url.rstrip("/")
        self._timeout = timeout
        self._cache: TTLCache[str, dict[str, float | bool]] = TTLCache(maxsize=1024, ttl=cache_ttl)
        self._headers_provider = headers_provider
        self._transport = _transport

    async def check_budget(self, tenant_id: str) -> tuple[bool, float, float]:
        """Check whether *tenant_id* is within its budget cap.

        Returns ``(allowed, current_spend, budget_cap)``.

        * ``allowed`` is ``True`` when the tenant may proceed.
        * On any error the method returns ``(True, 0.0, inf)`` (fail-open).
        * Results are cached per tenant for the configured TTL.
        """
        cached = self._cache.get(tenant_id)
        if cached is not None:
            return cached["allowed"], cached["spend"], cached["cap"]  # type: ignore[return-value]

        try:
            client_kwargs: dict[str, Any] = {"timeout": self._timeout}
            if self._transport is not None:
                client_kwargs["transport"] = httpx.MockTransport(self._transport)

            headers = self._headers_provider() if self._headers_provider is not None else None
            async with httpx.AsyncClient(**client_kwargs) as client:
                resp = await client.get(
                    f"{self._base_url}/dashboard/kpis",
                    params={"tenant_id": tenant_id},
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()
                spend = float(data.get("total_cost_usd", 0))
                cap = float(data.get("budget_cap_usd", float("inf")))
                allowed = spend < cap
                self._cache[tenant_id] = {"allowed": allowed, "spend": spend, "cap": cap}
                return allowed, spend, cap
        except Exception as exc:  # noqa: BLE001
            # Fail-open: Regulus unavailability must not block execution (D-12).
            # But a silent fail-open means budget governance can evaporate with
            # no trace — emit a warning so operators can see caps aren't being
            # enforced and alert on it.
            logger.warning(
                "budget check failed open for tenant %s: %s (%s) — spend cap NOT enforced",
                tenant_id,
                exc.__class__.__name__,
                exc,
            )
            return True, 0.0, float("inf")
