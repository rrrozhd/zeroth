"""Regulus economics data models.

Defines settings for connecting to the Regulus backend and a cost
attribution record that ties an LLM call to its cost event.
"""

from __future__ import annotations

from pydantic import BaseModel, SecretStr


class RegulusSettings(BaseModel):
    """Regulus backend connection settings.

    Configured via environment variables with ZEROTH_REGULUS__ prefix
    when nested inside ZerothSettings.
    """

    # Default-ENABLED (G1): the bundled in-process economic control plane is
    # wired out of the box so per-tenant spend caps are enforced in a default
    # deploy with no env flags. A fresh deploy auto-generates a strong ephemeral
    # signing secret for the mount (see core/service/app.py lifespan). Point at an
    # external Regulus by setting ZEROTH_REGULUS__BASE_URL, or turn the plane off
    # with ZEROTH_REGULUS__ENABLED=false.
    enabled: bool = True
    base_url: str = "http://localhost:8000/v1"
    api_key: SecretStr | None = None
    budget_cache_ttl: int = 30  # seconds
    request_timeout: float = 5.0
    # Budget-check failure mode. Default fail-OPEN (D-12): a backend outage
    # never blocks production workloads (logged at WARNING). Set fail-CLOSED
    # to DENY when the control plane is unreachable — tighter governance at
    # the cost of availability. Applies ONLY to the error path; a 200 with a
    # null cap stays unlimited regardless.
    fail_closed: bool = False
    # Optional per-run cumulative cost ceiling (USD). Enforced locally from the
    # run's own audit cost_usd, so it works even with the control plane disabled.
    # Post-hoc: a run halts on the NEXT node once cumulative spend crosses the
    # cap. ``None`` disables the ceiling.
    per_run_cap_usd: float | None = None


class CostAttribution(BaseModel):
    """Cost attribution dimensions for a single LLM call."""

    cost_usd: float
    cost_event_id: str
    node_id: str
    run_id: str
    tenant_id: str
    deployment_ref: str
