from __future__ import annotations

import base64
import inspect
from typing import Any

import pytest

from zeroth.integrations.langgraph.enforcement_protocol import _canonical
from zeroth.platform.signing import EnvHmacSigner
from zeroth.platform.storage import NullWorkspaceScopeContext
from zeroth.platform.storage.scoped_table import BoundStructuredTable
from zeroth.platform.storage.json import to_json_value
from zeroth.service.langgraph_gateway import enforcement as _enforcement  # noqa: F401
from zeroth.service.langgraph_gateway.enforcement_store import (
    LangGraphEnforcementRepository,
    StoredCapabilityEvidenceProvider,
)


class _AttestationRows:
    def __init__(self, row: dict[str, Any]) -> None:
        self.scope_context = NullWorkspaceScopeContext(tenant_id="tenant-a")
        self._row = row

    async def get_attestation_by_run_id(
        self, deployment_ref: str, governance_run_id: str
    ) -> dict[str, Any]:
        return self._row


def test_langgraph_repository_constructor_requires_scope_context() -> None:
    parameters = inspect.signature(LangGraphEnforcementRepository).parameters

    assert "scope_context" in parameters
    assert parameters["scope_context"].default is inspect.Parameter.empty


async def test_foreign_langgraph_attestation_matches_unknown_scope(async_database) -> None:
    owner = LangGraphEnforcementRepository(
        async_database, NullWorkspaceScopeContext(tenant_id="tenant-a")
    )
    foreign = LangGraphEnforcementRepository(
        async_database, NullWorkspaceScopeContext(tenant_id="tenant-b")
    )
    payload = {
        "tenant_id": "tenant-a",
        "deployment_ref": "deployment-a",
        "run_id": "shared-run",
        "correlation_id": "shared-correlation",
        "governance_level": "enforced",
        "observed_at": "2026-08-12T00:00:00+00:00",
        "graph_version": "graph:v1",
        "adapter_version": "adapter:v1",
        "inventory_fingerprint": "fingerprint",
        "tool_manifest_complete": True,
    }
    await owner.save_attestation(payload, b"signature", "key-1", "hmac-sha256")

    assert await foreign.get_attestation_by_run_id("deployment-a", "shared-run") is None
    assert await foreign.get_attestation_by_run_id("deployment-a", "unknown-run") is None


def test_evidence_provider_rejects_repository_bound_to_another_tenant(async_database) -> None:
    repository = LangGraphEnforcementRepository(
        async_database, NullWorkspaceScopeContext(tenant_id="tenant-b")
    )
    signer = EnvHmacSigner(key_id="test", keys={"test": b"test-key"})

    with pytest.raises(ValueError, match="tenant_id does not match repository scope"):
        StoredCapabilityEvidenceProvider(
            repository,
            signer,
            tenant_id="tenant-a",
            deployment_ref="deployment-a",
        )


@pytest.mark.parametrize(
    ("payload_override", "case"),
    [
        ({"tenant_id": "tenant-b"}, "foreign tenant"),
        ({"deployment_ref": "deployment-b"}, "foreign deployment"),
    ],
)
async def test_evidence_provider_rejects_signed_foreign_scope(
    payload_override: dict[str, str], case: str
) -> None:
    payload = {
        "tenant_id": "tenant-a",
        "deployment_ref": "deployment-a",
        "run_id": "run-a",
        "correlation_id": "correlation-a",
        "governance_level": "enforced",
        "observed_at": "2026-08-12T00:00:00+00:00",
        "graph_version": "graph:v1",
        "adapter_version": "adapter:v1",
        "inventory_fingerprint": "fingerprint",
        "tool_manifest_complete": True,
    }
    payload.update(payload_override)
    signer = EnvHmacSigner(key_id="test", keys={"test": b"test-key"})
    row = {
        "payload_json": to_json_value(payload),
        "signature": base64.b64encode(signer.sign(_canonical(payload))).decode("ascii"),
        "signing_key_id": signer.key_id(),
    }
    provider = StoredCapabilityEvidenceProvider(
        _AttestationRows(row),  # type: ignore[arg-type]
        signer,
        tenant_id="tenant-a",
        deployment_ref="deployment-a",
    )

    evidence = await provider.evidence_for_governance_run("run-a")

    assert evidence is not None, case
    assert evidence.signature_valid is False, case


async def test_decision_count_uses_scoped_aggregate(async_database, monkeypatch) -> None:
    repository = LangGraphEnforcementRepository(
        async_database, NullWorkspaceScopeContext(tenant_id="tenant-a")
    )
    calls: list[dict[str, object]] = []
    original = BoundStructuredTable.count

    async def recording_count(self, **kwargs):
        calls.append(kwargs)
        return await original(self, **kwargs)

    monkeypatch.setattr(BoundStructuredTable, "count", recording_count)

    assert await repository.count_decisions() == 0
    assert calls == [{"where": {}}]


async def test_legacy_attestation_ambiguity_reads_at_most_two_rows(
    async_database, monkeypatch
) -> None:
    repository = LangGraphEnforcementRepository(
        async_database, NullWorkspaceScopeContext(tenant_id="tenant-a")
    )
    calls: list[dict[str, object]] = []
    original = BoundStructuredTable.select

    async def recording_select(self, **kwargs):
        calls.append(kwargs)
        return await original(self, **kwargs)

    monkeypatch.setattr(BoundStructuredTable, "select", recording_select)

    with pytest.warns(DeprecationWarning):
        assert await repository.get_attestation("deployment-a", "correlation-a") is None
    assert calls == [
        {
            "where": {
                "deployment_ref": "deployment-a",
                "correlation_id": "correlation-a",
            },
            "limit": 2,
        }
    ]
