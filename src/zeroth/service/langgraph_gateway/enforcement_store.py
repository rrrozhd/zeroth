"""Persistence and signature verification for LangGraph enforcement evidence."""

from __future__ import annotations

import base64
import warnings
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from zeroth.contracts.langgraph_gateway.models import GovernanceLevel, RunCapabilityEvidence
from zeroth.platform.signing import SigningKeyProvider
from zeroth.platform.storage import (
    SERVICE_SCOPE_REGISTRY,
    AsyncDatabase,
    NullWorkspaceScopeContext,
    ScopeContext,
    ScopedTable,
)
from zeroth.platform.storage.json import from_json_value, to_json_value
from zeroth.service.langgraph_gateway.enforcement import (
    DecisionResponseV1,
    EnforcementBoundaryError,
    HeartbeatV1,
    InventoryRegistrationV1,
    _canonical,
)


class LangGraphEnforcementRepository:
    """Transactional persistence for decisions, inventories, and attestations."""

    def __init__(
        self,
        database: AsyncDatabase,
        scope_context: ScopeContext | NullWorkspaceScopeContext,
    ) -> None:
        if type(scope_context) not in {ScopeContext, NullWorkspaceScopeContext}:
            raise TypeError("scope_context must be a trusted tenant scope")
        self._database = database
        self._scope_context = scope_context
        self._decisions = ScopedTable(
            database, SERVICE_SCOPE_REGISTRY, "service.langgraph_decisions", scope_context
        )
        self._inventories = ScopedTable(
            database, SERVICE_SCOPE_REGISTRY, "service.langgraph_inventories", scope_context
        )
        self._attestations = ScopedTable(
            database,
            SERVICE_SCOPE_REGISTRY,
            "service.langgraph_run_attestations",
            scope_context,
        )

    @classmethod
    def for_default_compatibility(cls, database: AsyncDatabase) -> LangGraphEnforcementRepository:
        return cls(database, NullWorkspaceScopeContext.for_default_compatibility())

    def _validate_tenant(self, tenant_id: object) -> None:
        if tenant_id != self._scope_context.tenant_id:
            raise ValueError("tenant_id does not match bound scope")

    async def save_decision(
        self,
        key: str,
        deployment_ref: str,
        action_hash: str,
        response: DecisionResponseV1,
    ) -> DecisionResponseV1:
        async with self._decisions.transaction(write_lock=True) as decisions:
            await decisions.insert_if_absent(
                {
                    "idempotency_key": key,
                    "deployment_ref": deployment_ref,
                    "action_hash": action_hash,
                    "response_json": to_json_value(response.model_dump(mode="json")),
                    "created_at": datetime.now(tz=UTC).isoformat(),
                },
                conflict_columns=("tenant_id", "idempotency_key"),
            )
            row = await decisions.select_one(
                where={"idempotency_key": key},
                columns=("deployment_ref", "action_hash", "response_json"),
            )
            if row is None:
                raise RuntimeError("idempotent decision row was not persisted")
            if row["deployment_ref"] != deployment_ref or row["action_hash"] != action_hash:
                raise EnforcementBoundaryError("zeroth.idempotency_conflict", status_code=409)
            return DecisionResponseV1.model_validate(from_json_value(row["response_json"]))

    async def count_decisions(self) -> int:
        async with self._decisions.transaction() as decisions:
            rows = await decisions.select(columns=("idempotency_key",))
        return len(rows)

    async def register_inventory(self, request: InventoryRegistrationV1) -> None:
        self._validate_tenant(request.tenant_id)
        entries_json = to_json_value([entry.model_dump(mode="json") for entry in request.entries])
        now = datetime.now(tz=UTC).isoformat()
        values = {
            "deployment_ref": request.deployment_ref,
            "graph_version": request.graph_version,
            "adapter_version": request.adapter_version,
            "inventory_fingerprint": request.inventory_fingerprint,
            "coverage": request.coverage.value,
            "entries_json": entries_json,
            "registered_at": now,
            "heartbeat_at": now,
        }
        identity = {
            key: values[key]
            for key in (
                "deployment_ref",
                "graph_version",
                "adapter_version",
                "inventory_fingerprint",
            )
        }
        async with self._inventories.transaction(write_lock=True) as inventories:
            inserted = await inventories.insert_if_absent(
                values,
                conflict_columns=(
                    "tenant_id",
                    "deployment_ref",
                    "graph_version",
                    "adapter_version",
                    "inventory_fingerprint",
                ),
            )
            if not inserted:
                await inventories.update(
                    {
                        "coverage": values["coverage"],
                        "entries_json": entries_json,
                        "registered_at": now,
                        "heartbeat_at": now,
                    },
                    where=identity,
                )

    async def get_inventory(
        self,
        deployment_ref: str,
        graph_version: str,
        adapter_version: str,
        fingerprint: str,
    ) -> dict[str, Any] | None:
        return await self._inventories.select_one(
            where={
                "deployment_ref": deployment_ref,
                "graph_version": graph_version,
                "adapter_version": adapter_version,
                "inventory_fingerprint": fingerprint,
            }
        )

    async def heartbeat(self, request: HeartbeatV1) -> str | None:
        self._validate_tenant(request.tenant_id)
        identity = {
            "deployment_ref": request.deployment_ref,
            "graph_version": request.graph_version,
            "adapter_version": request.adapter_version,
            "inventory_fingerprint": request.inventory_fingerprint,
        }
        async with self._inventories.transaction(write_lock=True) as inventories:
            row = await inventories.select_one(where=identity, columns=("coverage",))
            if row is None:
                return None
            await inventories.update(
                {"heartbeat_at": datetime.now(tz=UTC).isoformat()}, where=identity
            )
        return str(row["coverage"])

    async def save_attestation(
        self, payload: Mapping[str, Any], signature: bytes, key_id: str, algorithm: str
    ) -> None:
        self._validate_tenant(payload.get("tenant_id"))
        async with self._attestations.transaction(write_lock=True) as attestations:
            await attestations.insert_if_absent(
                {
                    "deployment_ref": payload["deployment_ref"],
                    "run_id": payload["run_id"],
                    "correlation_id": payload["correlation_id"],
                    "payload_json": to_json_value(dict(payload)),
                    "signature": base64.b64encode(signature).decode("ascii"),
                    "signing_key_id": key_id,
                    "algorithm": algorithm,
                },
                conflict_columns=("tenant_id", "deployment_ref", "run_id"),
            )
            row = await attestations.select_one(
                where={
                    "deployment_ref": payload["deployment_ref"],
                    "run_id": payload["run_id"],
                },
                columns=("payload_json",),
            )
            if row is None:
                raise RuntimeError("run attestation was not persisted")
            stored = from_json_value(row["payload_json"])
            stable_keys = set(payload) - {"observed_at"}
            if any(stored.get(key) != payload[key] for key in stable_keys):
                raise EnforcementBoundaryError("zeroth.attestation_conflict", status_code=409)

    async def get_attestation(
        self, deployment_ref: str, correlation_id: str
    ) -> dict[str, Any] | None:
        """Return the sole attestation for a legacy correlation ID, if unambiguous."""
        warnings.warn(
            "get_attestation() is deprecated; use get_attestation_by_run_id()",
            DeprecationWarning,
            stacklevel=2,
        )
        async with self._attestations.transaction() as attestations:
            rows = await attestations.select(
                where={"deployment_ref": deployment_ref, "correlation_id": correlation_id}
            )
        return rows[0] if len(rows) == 1 else None

    async def get_attestation_by_run_id(
        self, deployment_ref: str, governance_run_id: str
    ) -> dict[str, Any] | None:
        """Return evidence for an exact signed governance run ID."""
        return await self._attestations.select_one(
            where={"deployment_ref": deployment_ref, "run_id": governance_run_id}
        )


class StoredCapabilityEvidenceProvider:
    """Verify stored server signatures before returning capability evidence."""

    def __init__(
        self,
        repository: LangGraphEnforcementRepository,
        signer: SigningKeyProvider,
        *,
        tenant_id: str,
        deployment_ref: str,
    ) -> None:
        self._repository = repository
        self._signer = signer
        self._tenant_id = tenant_id
        self._deployment_ref = deployment_ref

    async def evidence_for_run(self, correlation_id: str) -> RunCapabilityEvidence | None:
        """Return evidence for a legacy correlation ID, if unambiguous."""
        warnings.warn(
            "evidence_for_run() is deprecated; use evidence_for_governance_run()",
            DeprecationWarning,
            stacklevel=2,
        )
        row = await self._repository.get_attestation(self._deployment_ref, correlation_id)
        return self._evidence_from_row(row, identity="correlation_id", expected=correlation_id)

    async def evidence_for_governance_run(
        self, governance_run_id: str
    ) -> RunCapabilityEvidence | None:
        """Return evidence for an exact signed governance run ID."""
        row = await self._repository.get_attestation_by_run_id(
            self._deployment_ref, governance_run_id
        )
        return self._evidence_from_row(row, identity="run_id", expected=governance_run_id)

    def _evidence_from_row(
        self,
        row: dict[str, Any] | None,
        *,
        identity: str,
        expected: str,
    ) -> RunCapabilityEvidence | None:
        if row is None:
            return None
        payload = from_json_value(row["payload_json"])
        try:
            signature = base64.b64decode(row["signature"], validate=True)
            valid = (
                self._signer.verify(_canonical(payload), signature, row["signing_key_id"]) is True
                and payload.get(identity) == expected
            )
        except Exception:
            valid = False
        return RunCapabilityEvidence(
            correlation_id=payload["correlation_id"],
            run_id=payload.get("run_id"),
            governance_level=GovernanceLevel(payload["governance_level"]),
            observed_at=datetime.fromisoformat(payload["observed_at"]),
            graph_version=payload["graph_version"],
            adapter_version=payload["adapter_version"],
            inventory_fingerprint=payload["inventory_fingerprint"],
            signature_valid=valid,
            tool_manifest_complete=bool(payload["tool_manifest_complete"]),
        )


__all__ = ["LangGraphEnforcementRepository", "StoredCapabilityEvidenceProvider"]
