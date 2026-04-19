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

    enabled: bool = False
    base_url: str = "http://localhost:8000/v1"
    api_key: SecretStr | None = None
    budget_cache_ttl: int = 30  # seconds
    request_timeout: float = 5.0


class CostAttribution(BaseModel):
    """Cost attribution dimensions for a single LLM call."""

    cost_usd: float
    cost_event_id: str
    node_id: str
    run_id: str
    tenant_id: str
    deployment_ref: str
