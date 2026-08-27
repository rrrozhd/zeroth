"""WS-D: deployment attestations are signed on deploy; dual-check verification."""

from __future__ import annotations

from pydantic import BaseModel
import pytest

from tests.graph.test_models import build_graph
from zeroth.contracts.registry import ContractRegistry
from zeroth.service.deployments import (
    Deployment,
    DeploymentEngineMode,
    DeploymentError,
    DeploymentService,
    SQLiteDeploymentRepository,
)
from zeroth.service.deployments.provenance import (
    build_attestation_payload,
    verify_attestation_full,
)
from zeroth.contracts.graph import GraphRepository
from zeroth.platform.signing import EnvHmacSigner, SigningKeyProvider


class _In(BaseModel):
    value: int


class _Out(BaseModel):
    value: int


async def _signed_service(sqlite_db, signer: SigningKeyProvider | None) -> DeploymentService:
    contract_registry = ContractRegistry.for_default_compatibility(sqlite_db)
    await contract_registry.register(_In, name="contract://input")
    await contract_registry.register(_Out, name="contract://output")
    return DeploymentService(
        graph_repository=GraphRepository(sqlite_db),
        deployment_repository=SQLiteDeploymentRepository(sqlite_db),
        contract_registry=contract_registry,
        signer=signer,
    )


async def _deploy(service: DeploymentService, ref: str = "svc"):
    graph = await service.graph_repository.create(build_graph())
    await service.graph_repository.publish(graph.graph_id, graph.version)
    return await service.deploy(ref, graph.graph_id, graph.version)


def _flip_hex(value: str) -> str:
    head = "1" if value[0] == "0" else "0"
    return head + value[1:]


async def test_attestation_signed_on_deploy(sqlite_db) -> None:
    signer = EnvHmacSigner(key_id="k1", keys={"k1": b"deploy-key"})
    deployed = await _deploy(await _signed_service(sqlite_db, signer))

    assert deployed.attestation_signature
    assert deployed.attestation_signing_key_id == "k1"
    assert deployed.attestation_algorithm == "HS256"

    mismatches, signature_ok = verify_attestation_full(
        deployed, build_attestation_payload(deployed), signer
    )
    assert mismatches == []
    assert signature_ok is True
    assert build_attestation_payload(deployed)["attestation_payload_version"] == 2
    assert build_attestation_payload(deployed)["engine_mode"] == "token"


def test_version_one_attestation_payload_remains_byte_compatible() -> None:
    deployment = Deployment(
        deployment_id="legacy-id",
        deployment_ref="legacy-ref",
        graph_id="graph-1",
        graph_version=1,
        graph_version_ref="graph-1@1",
        serialized_graph="{}",
        engine_mode=DeploymentEngineMode.LEGACY,
        attestation_payload_version=1,
    )

    payload = build_attestation_payload(deployment)

    assert "attestation_payload_version" not in payload
    assert "engine_mode" not in payload


async def test_signature_persists_and_hydrates(sqlite_db) -> None:
    signer = EnvHmacSigner(key_id="k1", keys={"k1": b"deploy-key"})
    service = await _signed_service(sqlite_db, signer)
    deployed = await _deploy(service)

    reloaded = await service.deployment_repository.get("svc")
    assert reloaded is not None
    assert reloaded.attestation_signature == deployed.attestation_signature
    assert reloaded.attestation_signing_key_id == "k1"
    assert reloaded.attestation_algorithm == "HS256"


async def test_dual_check_payload_tamper_trips_digest_not_signature(sqlite_db) -> None:
    signer = EnvHmacSigner(key_id="k1", keys={"k1": b"deploy-key"})
    deployed = await _deploy(await _signed_service(sqlite_db, signer))
    original = build_attestation_payload(deployed)

    # Tamper the snapshot bytes but leave the persisted digest+signature intact:
    # the OLD signature still validates over the unchanged attestation_digest,
    # while the digest recompute catches the drift. Neither axis masks the other.
    tampered = deployed.model_copy(
        update={"serialized_graph": deployed.serialized_graph + " tampered"}
    )
    mismatches, signature_ok = verify_attestation_full(tampered, original, signer)
    assert "graph_snapshot_digest" in mismatches
    assert signature_ok is True


async def test_dual_check_signature_flip_trips_signature_not_digest(sqlite_db) -> None:
    signer = EnvHmacSigner(key_id="k1", keys={"k1": b"deploy-key"})
    deployed = await _deploy(await _signed_service(sqlite_db, signer))
    original = build_attestation_payload(deployed)

    tampered = deployed.model_copy(
        update={"attestation_signature": _flip_hex(deployed.attestation_signature)}
    )
    mismatches, signature_ok = verify_attestation_full(tampered, original, signer)
    assert mismatches == []  # content untouched -> digest axis passes
    assert signature_ok is False  # flipped signature -> signature axis fails


async def test_unsigned_legacy_attestation_is_three_state_none(sqlite_db) -> None:
    service = await _signed_service(sqlite_db, None)
    service.deployment_mode = "local"
    graph = build_graph()
    local_read_only = graph.model_copy(update={"nodes": [graph.nodes[0]], "edges": []})
    stored = await service.graph_repository.create(local_read_only)
    await service.graph_repository.publish(stored.graph_id, stored.version)
    deployed = await service.deploy("svc", stored.graph_id, stored.version)
    assert deployed.attestation_signature is None
    assert deployed.attestation_signing_key_id is None

    mismatches, signature_ok = verify_attestation_full(
        deployed, build_attestation_payload(deployed), None
    )
    assert mismatches == []
    assert signature_ok is None  # unsigned-legacy: neither verified nor tampered


async def test_production_deployment_fails_closed_without_signer(sqlite_db) -> None:
    service = await _signed_service(sqlite_db, None)
    service.deployment_mode = "production"

    with pytest.raises(DeploymentError, match="signed audit readiness"):
        await _deploy(service)


async def test_consequential_local_deployment_fails_closed_without_signer(sqlite_db) -> None:
    service = await _signed_service(sqlite_db, None)
    service.deployment_mode = "local"
    graph = build_graph()
    consequential = graph.model_copy(
        update={
            "nodes": [
                node.model_copy(update={"capability_bindings": ["external_api_call"]})
                for node in graph.nodes
            ]
        }
    )
    stored = await service.graph_repository.create(consequential)
    await service.graph_repository.publish(stored.graph_id, stored.version)

    with pytest.raises(DeploymentError, match="signed audit readiness"):
        await service.deploy("consequential", stored.graph_id, stored.version)
