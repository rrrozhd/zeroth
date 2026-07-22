"""Regulus economics data models.

Defines settings for connecting to the Regulus backend and a cost
attribution record that ties an LLM call to its cost event.
"""

from __future__ import annotations

from pydantic import BaseModel

# RegulusSettings is composed into ZerothSettings as a field, so its definition
# lives in the platform layer; this legacy path republishes the same class.
from zeroth.platform.config.models import RegulusSettings

__all__ = ["CostAttribution", "RegulusSettings"]


class CostAttribution(BaseModel):
    """Cost attribution dimensions for a single LLM call."""

    cost_usd: float
    cost_event_id: str
    node_id: str
    run_id: str
    tenant_id: str
    deployment_ref: str
