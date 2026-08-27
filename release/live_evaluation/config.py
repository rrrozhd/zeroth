"""Strict, non-secret configuration for the live Studio evaluation campaign."""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

_SECRET_REF = re.compile(r"^[a-z][a-z0-9.-]{2,80}$")


class CampaignConfig(BaseModel):
    """Pin the first campaign to a dedicated tenant and bounded external spend."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    campaign_id: str = Field(pattern=r"^evaluation-[a-z0-9-]{3,64}$")
    tenant_id: str
    provider: Literal["openai"]
    model: Literal["openai/gpt-4o-mini"]
    embedding_model: Literal["openai/text-embedding-3-small"]
    vector_backend: Literal["chroma"]
    campaign_budget_usd: Decimal = Field(gt=0)
    per_run_cap_usd: Decimal = Field(gt=0)
    provider_secret_ref: str
    artifact_root: Path
    action_sink_root: Path

    @field_validator("tenant_id")
    @classmethod
    def _dedicated_tenant(cls, value: str) -> str:
        if value == "default":
            raise ValueError("a dedicated evaluation tenant is required")
        if not value.startswith("evaluation-"):
            raise ValueError("tenant_id must start with 'evaluation-'")
        if not re.fullmatch(r"[a-z0-9-]{12,80}", value):
            raise ValueError("tenant_id contains unsafe characters")
        return value

    @field_validator("campaign_budget_usd")
    @classmethod
    def _campaign_budget(cls, value: Decimal) -> Decimal:
        if value > Decimal("10.00"):
            raise ValueError("campaign budget exceeds the approved safety ceiling")
        return value

    @field_validator("per_run_cap_usd")
    @classmethod
    def _per_run_cap(cls, value: Decimal) -> Decimal:
        if value > Decimal("0.25"):
            raise ValueError("per-run cap exceeds the approved safety ceiling")
        return value

    @field_validator("provider_secret_ref")
    @classmethod
    def _credential_reference(cls, value: str) -> str:
        if not _SECRET_REF.fullmatch(value):
            raise ValueError("provider credential must be a logical secret reference")
        return value

    @model_validator(mode="after")
    def _scoped_action_sink(self) -> CampaignConfig:
        artifact_root = self.artifact_root.expanduser().resolve(strict=False)
        action_sink_root = self.action_sink_root.expanduser().resolve(strict=False)
        if action_sink_root == artifact_root or not action_sink_root.is_relative_to(artifact_root):
            raise ValueError("action sink must be a child of the artifact root")
        return self

    def resolve_paid(
        self,
        secret_provider: object,
        *,
        acknowledge_external_cost: bool,
    ) -> ResolvedPaidCampaign:
        """Resolve the provider secret only after an explicit cost acknowledgement."""
        if not acknowledge_external_cost:
            raise ValueError("paid evaluation requires explicit external-cost acknowledgement")
        resolver = getattr(secret_provider, "resolve_secret", None)
        if not callable(resolver):
            raise TypeError("secret_provider must implement resolve_secret")
        provider_key = resolver(self.provider_secret_ref, tenant_id=self.tenant_id)
        if not provider_key:
            raise ValueError(
                "required provider credential logical secret "
                f"{self.provider_secret_ref} is unresolved"
            )
        return ResolvedPaidCampaign(config=self, provider_key=SecretStr(provider_key))


class ResolvedPaidCampaign(BaseModel):
    """Runtime-only paid configuration; the provider credential never serializes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    config: CampaignConfig
    provider_key: SecretStr = Field(exclude=True)
