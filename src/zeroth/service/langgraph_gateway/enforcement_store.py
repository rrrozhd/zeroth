"""Persistence and signature verification for LangGraph enforcement evidence."""

from __future__ import annotations

import base64
import warnings
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from zeroth.contracts.langgraph_gateway.models import GovernanceLevel, RunCapabilityEvidence
from zeroth.core.signing import SigningKeyProvider
from zeroth.platform.storage import AsyncDatabase
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

    def __init__(self, database: AsyncDatabase) -> None:
        self._database = database

    async def save_decision(
        self,
        tenant_id: str,
        key: str,
        deployment_ref: str,
        action_hash: str,
        response: DecisionResponseV1,
    ) -> DecisionResponseV1:
        async with self._database.transaction(write_lock=True) as connection:
            await connection.execute(
                "INSERT INTO langgraph_decisions "
                "(tenant_id, idempotency_key, deployment_ref, action_hash, "
                "response_json, created_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(tenant_id, idempotency_key) DO NOTHING",
                (
                    tenant_id,
                    key,
                    deployment_ref,
                    action_hash,
                    to_json_value(response.model_dump(mode="json")),
                    datetime.now(tz=UTC).isoformat(),
                ),
            )
            row = await connection.fetch_one(
                "SELECT deployment_ref, action_hash, response_json FROM langgraph_decisions "
                "WHERE tenant_id = ? AND idempotency_key = ?",
                (tenant_id, key),
            )
            if row is None:
                raise RuntimeError("idempotent decision row was not persisted")
            if row["deployment_ref"] != deployment_ref or row["action_hash"] != action_hash:
                raise EnforcementBoundaryError("zeroth.idempotency_conflict", status_code=409)
            return DecisionResponseV1.model_validate(from_json_value(row["response_json"]))

    async def count_decisions(self) -> int:
        async with self._database.transaction() as connection:
            row = await connection.fetch_one("SELECT COUNT(*) AS count FROM langgraph_decisions")
        return int(row["count"] if row else 0)

    async def register_inventory(self, request: InventoryRegistrationV1) -> None:
        entries_json = to_json_value([entry.model_dump(mode="json") for entry in request.entries])
        now = datetime.now(tz=UTC).isoformat()
        async with self._database.transaction(write_lock=True) as connection:
            await connection.execute(
                "INSERT INTO langgraph_inventories "
                "(tenant_id, deployment_ref, graph_version, adapter_version, "
                "inventory_fingerprint, coverage, entries_json, registered_at, heartbeat_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(tenant_id, deployment_ref, graph_version, adapter_version, "
                "inventory_fingerprint) DO UPDATE SET coverage = excluded.coverage, "
                "entries_json = excluded.entries_json, registered_at = excluded.registered_at, "
                "heartbeat_at = excluded.heartbeat_at",
                (
                    request.tenant_id,
                    request.deployment_ref,
                    request.graph_version,
                    request.adapter_version,
                    request.inventory_fingerprint,
                    request.coverage.value,
                    entries_json,
                    now,
                    now,
                ),
            )

    async def get_inventory(
        self,
        tenant_id: str,
        deployment_ref: str,
        graph_version: str,
        adapter_version: str,
        fingerprint: str,
    ) -> dict[str, Any] | None:
        async with self._database.transaction() as connection:
            return await connection.fetch_one(
                "SELECT * FROM langgraph_inventories WHERE tenant_id = ? AND deployment_ref = ? "
                "AND graph_version = ? AND adapter_version = ? AND inventory_fingerprint = ?",
                (tenant_id, deployment_ref, graph_version, adapter_version, fingerprint),
            )

    async def heartbeat(self, request: HeartbeatV1) -> str | None:
        async with self._database.transaction(write_lock=True) as connection:
            row = await connection.fetch_one(
                "SELECT coverage FROM langgraph_inventories WHERE tenant_id = ? "
                "AND deployment_ref = ? AND graph_version = ? AND adapter_version = ? "
                "AND inventory_fingerprint = ?",
                (
                    request.tenant_id,
                    request.deployment_ref,
                    request.graph_version,
                    request.adapter_version,
                    request.inventory_fingerprint,
                ),
            )
            if row is None:
                return None
            await connection.execute(
                "UPDATE langgraph_inventories SET heartbeat_at = ? WHERE tenant_id = ? "
                "AND deployment_ref = ? AND graph_version = ? AND adapter_version = ? "
                "AND inventory_fingerprint = ?",
                (
                    datetime.now(tz=UTC).isoformat(),
                    request.tenant_id,
                    request.deployment_ref,
                    request.graph_version,
                    request.adapter_version,
                    request.inventory_fingerprint,
                ),
            )
        return str(row["coverage"])

    async def save_attestation(
        self, payload: Mapping[str, Any], signature: bytes, key_id: str, algorithm: str
    ) -> None:
        async with self._database.transaction(write_lock=True) as connection:
            await connection.execute(
                "INSERT INTO langgraph_run_attestations "
                "(tenant_id, deployment_ref, run_id, correlation_id, payload_json, signature, "
                "signing_key_id, algorithm) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(tenant_id, deployment_ref, run_id) DO NOTHING",
                (
                    payload["tenant_id"],
                    payload["deployment_ref"],
                    payload["run_id"],
                    payload["correlation_id"],
                    to_json_value(dict(payload)),
                    base64.b64encode(signature).decode("ascii"),
                    key_id,
                    algorithm,
                ),
            )
            row = await connection.fetch_one(
                "SELECT payload_json FROM langgraph_run_attestations WHERE tenant_id = ? "
                "AND deployment_ref = ? AND run_id = ?",
                (
                    payload["tenant_id"],
                    payload["deployment_ref"],
                    payload["run_id"],
                ),
            )
            if row is None:
                raise RuntimeError("run attestation was not persisted")
            stored = from_json_value(row["payload_json"])
            stable_keys = set(payload) - {"observed_at"}
            if any(stored.get(key) != payload[key] for key in stable_keys):
                raise EnforcementBoundaryError("zeroth.attestation_conflict", status_code=409)

    async def get_attestation(
        self, tenant_id: str, deployment_ref: str, correlation_id: str
    ) -> dict[str, Any] | None:
        """Return the sole attestation for a legacy correlation ID, if unambiguous."""
        warnings.warn(
            "get_attestation() is deprecated; use get_attestation_by_run_id()",
            DeprecationWarning,
            stacklevel=2,
        )
        async with self._database.transaction() as connection:
            rows = await connection.fetch_all(
                "SELECT * FROM langgraph_run_attestations WHERE tenant_id = ? "
                "AND deployment_ref = ? AND correlation_id = ? LIMIT 2",
                (tenant_id, deployment_ref, correlation_id),
            )
        return rows[0] if len(rows) == 1 else None

    async def get_attestation_by_run_id(
        self, tenant_id: str, deployment_ref: str, governance_run_id: str
    ) -> dict[str, Any] | None:
        """Return evidence for an exact signed governance run ID."""
        async with self._database.transaction() as connection:
            return await connection.fetch_one(
                "SELECT * FROM langgraph_run_attestations WHERE tenant_id = ? "
                "AND deployment_ref = ? AND run_id = ?",
                (tenant_id, deployment_ref, governance_run_id),
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
        row = await self._repository.get_attestation(
            self._tenant_id, self._deployment_ref, correlation_id
        )
        return self._evidence_from_row(row, identity="correlation_id", expected=correlation_id)

    async def evidence_for_governance_run(
        self, governance_run_id: str
    ) -> RunCapabilityEvidence | None:
        """Return evidence for an exact signed governance run ID."""
        row = await self._repository.get_attestation_by_run_id(
            self._tenant_id, self._deployment_ref, governance_run_id
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
