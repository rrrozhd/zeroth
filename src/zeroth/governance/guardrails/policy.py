"""Durable, tenant-scoped ingress guardrail policy revisions."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from zeroth.governance.guardrails.config import GuardrailConfig
from zeroth.platform.primitives import utc_now
from zeroth.platform.storage import (
    SERVICE_SCOPE_REGISTRY,
    AsyncDatabase,
    NullWorkspaceScopeContext,
    ResourceOperation,
    ScopedTable,
    persistence_operation,
)
from zeroth.platform.storage.scoping import named_isolation_probe, persistence_surface

PolicyScope = Literal["tenant", "deployment"]
GuardrailField = Literal[
    "rate_limit_capacity",
    "rate_limit_refill_rate",
    "rate_limit_burst",
    "quota_daily_limit",
    "backpressure_queue_depth",
    "max_concurrency",
]


class GuardrailPolicyPatch(BaseModel):
    """A bounded partial policy; explicitly supplied null quota means unlimited."""

    model_config = ConfigDict(extra="forbid")

    rate_limit_capacity: float | None = Field(default=None, ge=1, le=1_000_000)
    rate_limit_refill_rate: float | None = Field(default=None, gt=0, le=100_000)
    rate_limit_burst: float | None = Field(default=None, ge=0, le=1_000_000)
    quota_daily_limit: int | None = Field(default=None, ge=1, le=1_000_000_000_000)
    backpressure_queue_depth: int | None = Field(default=None, ge=1, le=1_000_000)
    max_concurrency: int | None = Field(default=None, ge=1, le=10_000)
    reset_fields: tuple[GuardrailField, ...] = Field(default_factory=tuple)

    @field_validator("reset_fields")
    @classmethod
    def _canonicalize_reset_fields(
        cls, reset_fields: tuple[GuardrailField, ...]
    ) -> tuple[GuardrailField, ...]:
        """Reject duplicate tombstones and keep stored JSON deterministic."""
        if len(reset_fields) != len(set(reset_fields)):
            raise ValueError("reset_fields cannot contain duplicates")
        return tuple(sorted(reset_fields))

    @model_validator(mode="after")
    def _reject_null_for_bounded_controls(self) -> GuardrailPolicyPatch:
        supplied_fields = self.model_fields_set - {"reset_fields"}
        overlap = supplied_fields.intersection(self.reset_fields)
        if overlap:
            raise ValueError(f"fields cannot be both set and reset: {', '.join(sorted(overlap))}")
        for field_name in supplied_fields - {"quota_daily_limit"}:
            if getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null; omit it to inherit")
        return self

    def supplied_values(self) -> dict[str, float | int | None]:
        """Return only explicitly supplied fields, retaining explicit nulls."""
        return self.model_dump(mode="json", exclude_unset=True, exclude={"reset_fields"})

    def revision_values(self) -> dict[str, object]:
        """Return the immutable JSON payload, including explicit reset tombstones."""
        values: dict[str, object] = self.supplied_values()
        if self.reset_fields:
            values["reset_fields"] = list(self.reset_fields)
        return values

    def has_changes(self) -> bool:
        """Return whether this patch sets or resets at least one field."""
        return bool(self.supplied_values() or self.reset_fields)


class EffectiveGuardrailSettings(BaseModel):
    """Concrete product defaults after tenant and deployment composition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rate_limit_capacity: float = Field(default=10, ge=1, le=1_000_000)
    rate_limit_refill_rate: float = Field(default=1, gt=0, le=100_000)
    rate_limit_burst: float = Field(default=0, ge=0, le=1_000_000)
    quota_daily_limit: int | None = Field(default=None, ge=1, le=1_000_000_000_000)
    backpressure_queue_depth: int = Field(default=100, ge=1, le=1_000_000)
    max_concurrency: int = Field(default=8, ge=1, le=10_000)

    @property
    def bucket_capacity(self) -> float:
        """Return sustained capacity plus the explicitly configured burst."""
        return self.rate_limit_capacity + self.rate_limit_burst


class GuardrailPolicyRevision(BaseModel):
    """One immutable operator change in the append-only history."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    revision_id: str
    scope: PolicyScope
    deployment_ref: str | None
    policy: GuardrailPolicyPatch
    changed_by: str
    created_at: datetime


def effective_guardrails(
    *,
    baseline: EffectiveGuardrailSettings | None = None,
    tenant: GuardrailPolicyPatch | None = None,
    deployment: GuardrailPolicyPatch | None = None,
) -> EffectiveGuardrailSettings:
    """Compose fields in deployment > tenant > product-default order."""
    values = (baseline or EffectiveGuardrailSettings()).model_dump()
    for patch in (tenant, deployment):
        if patch is not None:
            values.update(patch.supplied_values())
    return EffectiveGuardrailSettings.model_validate(values)


def configured_guardrails(config: GuardrailConfig) -> EffectiveGuardrailSettings:
    """Convert deployment bootstrap configuration into the shared policy baseline."""
    return EffectiveGuardrailSettings(
        rate_limit_capacity=config.rate_limit_capacity,
        rate_limit_refill_rate=config.rate_limit_refill_rate,
        rate_limit_burst=getattr(config, "rate_limit_burst", 0),
        quota_daily_limit=config.quota_daily_limit,
        backpressure_queue_depth=config.backpressure_queue_depth,
        max_concurrency=getattr(config, "max_concurrency", 8),
    )


@persistence_surface(
    "service.guardrail_policy_revisions",
    probe=named_isolation_probe("_drive_guardrail_policy_revisions"),
)
class GuardrailPolicyRepository:
    """Append-only policy history structurally bound to one trusted tenant."""

    def __init__(
        self,
        database: AsyncDatabase,
        *,
        baseline: EffectiveGuardrailSettings | None = None,
    ) -> None:
        self._bind(
            database,
            NullWorkspaceScopeContext.for_default_compatibility(),
            baseline=baseline,
        )

    @classmethod
    def scoped(
        cls,
        database: AsyncDatabase,
        scope_context: NullWorkspaceScopeContext,
        *,
        baseline: EffectiveGuardrailSettings | None = None,
    ) -> GuardrailPolicyRepository:
        """Bind append-only policy history to one trusted tenant-wide scope."""
        if type(scope_context) is not NullWorkspaceScopeContext:
            raise TypeError("scope_context must be a NullWorkspaceScopeContext")
        repository = cls.__new__(cls)
        repository._bind(database, scope_context, baseline=baseline)
        return repository

    def _bind(
        self,
        database: AsyncDatabase,
        scope_context: NullWorkspaceScopeContext,
        *,
        baseline: EffectiveGuardrailSettings | None,
    ) -> None:
        """Install the scoped table and shared configured composition baseline."""
        self._baseline = baseline or EffectiveGuardrailSettings()
        self._revisions = ScopedTable(
            database,
            SERVICE_SCOPE_REGISTRY,
            "service.guardrail_policy_revisions",
            scope_context,
        )

    @persistence_operation(ResourceOperation.CREATE)
    async def append(
        self,
        *,
        scope: PolicyScope,
        policy: GuardrailPolicyPatch,
        changed_by: str,
        deployment_ref: str | None = None,
    ) -> GuardrailPolicyRevision:
        """Append and return an immutable tenant or deployment revision."""
        normalized_ref = _validate_scope(scope, deployment_ref)
        actor = changed_by.strip()
        if not actor:
            raise ValueError("changed_by must be non-empty")
        if not policy.has_changes():
            raise ValueError("policy must change at least one field")
        values = {
            "revision_id": uuid4().hex,
            "scope_type": scope,
            "deployment_ref": normalized_ref,
            "policy_json": json.dumps(
                policy.revision_values(), sort_keys=True, separators=(",", ":")
            ),
            "changed_by": actor,
            "created_at": utc_now().isoformat(),
        }
        async with self._revisions.transaction(write_lock=True) as revisions:
            row = await revisions.insert(values)
        return _row_to_revision(row)

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def history(
        self,
        *,
        scope: PolicyScope | None = None,
        deployment_ref: str | None = None,
    ) -> list[GuardrailPolicyRevision]:
        """Return this tenant's immutable revisions in deterministic order."""
        where: dict[str, object] = {}
        if scope is not None:
            where["scope_type"] = scope
            where["deployment_ref"] = _validate_scope(scope, deployment_ref)
        elif deployment_ref is not None:
            raise ValueError("deployment_ref requires scope='deployment'")
        async with self._revisions.transaction() as revisions:
            rows = await revisions.select(
                where=where,
                order_by=("revision_order",),
            )
        return [_row_to_revision(row) for row in rows]

    @persistence_operation(ResourceOperation.READ)
    async def effective(self, deployment_ref: str) -> EffectiveGuardrailSettings:
        """Fold immutable partial revisions over the configured baseline."""
        if not deployment_ref.strip():
            raise ValueError("deployment_ref must be non-empty")
        revisions = await self.history()
        return effective_guardrails(
            baseline=self._baseline,
            tenant=_fold_revisions(
                [revision for revision in revisions if revision.scope == "tenant"]
            ),
            deployment=_fold_revisions(
                [
                    revision
                    for revision in revisions
                    if revision.scope == "deployment" and revision.deployment_ref == deployment_ref
                ]
            ),
        )

    @persistence_operation(ResourceOperation.READ)
    async def current(
        self,
        scope: PolicyScope,
        *,
        deployment_ref: str | None = None,
    ) -> GuardrailPolicyPatch | None:
        """Return composed active overrides for one exact scope."""
        return _fold_revisions(await self.history(scope=scope, deployment_ref=deployment_ref))

    @persistence_operation(ResourceOperation.READ)
    async def latest(
        self,
        scope: PolicyScope,
        *,
        deployment_ref: str | None = None,
    ) -> GuardrailPolicyRevision | None:
        """Return immutable metadata for the latest revision in one exact scope."""
        return await self._latest(scope, _validate_scope(scope, deployment_ref))

    async def _latest(
        self,
        scope: PolicyScope,
        deployment_ref: str,
    ) -> GuardrailPolicyRevision | None:
        async with self._revisions.transaction() as revisions:
            row = await revisions.select_one(
                where={"scope_type": scope, "deployment_ref": deployment_ref},
                order_by_desc=("revision_order",),
            )
        return None if row is None else _row_to_revision(row)


def _validate_scope(scope: PolicyScope, deployment_ref: str | None) -> str:
    if scope == "tenant":
        if deployment_ref not in (None, ""):
            raise ValueError("tenant policy cannot name a deployment")
        return ""
    if scope != "deployment":
        raise ValueError("scope must be 'tenant' or 'deployment'")
    if deployment_ref is None or not deployment_ref.strip():
        raise ValueError("deployment policy requires deployment_ref")
    return deployment_ref


def _fold_revisions(revisions: list[GuardrailPolicyRevision]) -> GuardrailPolicyPatch | None:
    """Compose active same-scope overrides while applying reset tombstones."""
    values: dict[str, float | int | None] = {}
    for revision in revisions:
        for field_name in revision.policy.reset_fields:
            values.pop(field_name, None)
        values.update(revision.policy.supplied_values())
    return GuardrailPolicyPatch.model_validate(values) if values else None


def _row_to_revision(row: dict[str, object]) -> GuardrailPolicyRevision:
    payload = json.loads(str(row["policy_json"]))
    if not isinstance(payload, dict):
        raise ValueError("stored guardrail policy must be an object")
    deployment_ref = str(row["deployment_ref"])
    return GuardrailPolicyRevision(
        revision_id=str(row["revision_id"]),
        scope=str(row["scope_type"]),  # type: ignore[arg-type]
        deployment_ref=deployment_ref or None,
        policy=GuardrailPolicyPatch.model_validate(payload),
        changed_by=str(row["changed_by"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
    )
