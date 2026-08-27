from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from release.live_evaluation.control_adapters import (
    BatchEmbeddingResult,
    DockerChromaInspector,
    HttpPaidProbeExecutor,
    HttpSignedAuditInspector,
    InstrumentedChromaCorpusSeeder,
    PersistentInstrumentedBatchEmbeddingExecutor,
)
from release.live_evaluation.control_gate import PaidProbeAuthorization
from release.live_evaluation.control_gate_runtime import SyntheticChromaDocument
from release.live_evaluation.evidence import EvidenceStore
from zeroth.integrations.memory.tenant_scoped import tenant_slug


def _authorization(kind: str) -> PaidProbeAuthorization:
    return PaidProbeAuthorization(
        kind=kind,  # type: ignore[arg-type]
        campaign_id="evaluation-campaign",
        tenant_id="tenant-a",
        operation_id=f"operation-{kind}",
        run_id=f"run-{kind}",
        credential_reference="llm.openai",
        authorization_event_id=f"authorization-{kind}",
    )


def _signed_readiness_reference(store: EvidenceStore) -> str:
    event_id = store.append_event(
        "control.audit-readiness.inspected",
        {"ready": True, "state": "signed", "signer_available": True},
    )
    return f"events.ndjson#{event_id}"


def test_http_signed_audit_inspector_requires_signed_ready_state(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "ready": True,
                "state": "signed",
                "deployment_mode": "local",
                "signing_required": True,
                "signer_available": True,
                "consequential_actions": True,
                "message": "signed",
            },
        )

    store = EvidenceStore(tmp_path / "evidence")
    inspector = HttpSignedAuditInspector(
        client=httpx.Client(
            base_url="http://127.0.0.1:7000",
            transport=httpx.MockTransport(handler),
            headers={"Authorization": "Bearer not-recorded"},
        ),
        store=store,
        signing_reference="evaluation.control.signing",
    )

    result = inspector.inspect()

    assert result.state == "signed"
    assert result.evidence_reference.startswith("events.ndjson#")
    assert requests[0].url.path == "/v1/audit-readiness"
    evidence = (store.root / "events.ndjson").read_text()
    assert "not-recorded" not in evidence
    assert "Authorization" not in evidence


@pytest.mark.parametrize(
    "payload",
    [
        {"ready": False, "state": "blocked_unsigned", "signer_available": False},
        {"ready": True, "state": "local_unsigned", "signer_available": False},
    ],
)
def test_http_signed_audit_inspector_fails_closed(
    payload: dict[str, object], tmp_path: Path
) -> None:
    inspector = HttpSignedAuditInspector(
        client=httpx.Client(
            base_url="http://127.0.0.1:7000",
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload)),
        ),
        store=EvidenceStore(tmp_path / "evidence"),
        signing_reference="evaluation.control.signing",
    )

    with pytest.raises(RuntimeError, match="signed audit readiness"):
        inspector.inspect()


def test_docker_chroma_inspector_accepts_running_container_with_responsive_api(
    tmp_path: Path,
) -> None:
    inspect_payload = [
        {
            "Id": "4c7f0c",
            "Name": "/zeroth-evaluation-chroma",
            "Config": {"Image": "chromadb/chroma:1.5.6"},
            "State": {"Running": True},
            "NetworkSettings": {
                "Ports": {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8121"}]}
            },
        }
    ]
    commands: list[tuple[str, ...]] = []

    def run(argv: tuple[str, ...]) -> tuple[int, str, str]:
        commands.append(argv)
        return 0, json.dumps(inspect_payload), ""

    store = EvidenceStore(tmp_path / "evidence")
    inspector = DockerChromaInspector(
        client=httpx.Client(
            base_url="http://127.0.0.1:8121",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"version": "1.0.0"})
            ),
        ),
        store=store,
        container_name="zeroth-evaluation-chroma",
        expected_image="chromadb/chroma:1.5.6",
        host="127.0.0.1",
        port=8121,
        command_runner=run,
    )

    result = inspector.inspect()

    assert result.image == "chromadb/chroma:1.5.6"
    assert result.instance_id == "4c7f0c"
    assert result.api_version == "1.0.0"
    assert commands == [("docker", "inspect", "zeroth-evaluation-chroma")]


@pytest.mark.parametrize(
    ("image", "host_ip", "health"),
    [
        ("chromadb/chroma:latest", "127.0.0.1", "healthy"),
        ("chromadb/chroma:1.5.6", "0.0.0.0", "healthy"),
        ("chromadb/chroma:1.5.6", "127.0.0.1", "unhealthy"),
    ],
)
def test_docker_chroma_inspector_rejects_untrusted_runtime_identity(
    image: str, host_ip: str, health: str, tmp_path: Path
) -> None:
    payload = [
        {
            "Id": "container-id",
            "Name": "/zeroth-evaluation-chroma",
            "Config": {"Image": image},
            "State": {"Running": True, "Health": {"Status": health}},
            "NetworkSettings": {"Ports": {"8000/tcp": [{"HostIp": host_ip, "HostPort": "8121"}]}},
        }
    ]
    inspector = DockerChromaInspector(
        client=httpx.Client(
            base_url="http://127.0.0.1:8121",
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json={"version": "1.5.6"})
            ),
        ),
        store=EvidenceStore(tmp_path / "evidence"),
        container_name="zeroth-evaluation-chroma",
        expected_image="chromadb/chroma:1.5.6",
        host="127.0.0.1",
        port=8121,
        command_runner=lambda _argv: (0, json.dumps(payload), ""),
    )

    with pytest.raises(RuntimeError, match="Chroma container"):
        inspector.inspect()


class _Collection:
    def __init__(self, name: str) -> None:
        self.name = name
        self.rows: dict[str, tuple[str, list[float], dict[str, str]]] = {
            "stale": (json.dumps("stale"), [9.0, 9.0], {})
        }

    def get(self, *, include: list[str] | None = None):
        ids = list(self.rows)
        return {
            "ids": ids,
            "documents": [self.rows[item][0] for item in ids],
            "embeddings": [self.rows[item][1] for item in ids],
            "metadatas": [self.rows[item][2] for item in ids],
        }

    def delete(self, *, ids: list[str]) -> None:
        for item in ids:
            self.rows.pop(item, None)

    def upsert(self, *, ids, documents, embeddings, metadatas) -> None:
        for row in zip(ids, documents, embeddings, metadatas, strict=True):
            item_id, document, embedding, metadata = row
            self.rows[item_id] = (document, embedding, metadata)


class _EmbeddingExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...], str, str]] = []

    def embed_once(self, *, model, inputs, operation_id, run_id):
        self.calls.append((model, inputs, operation_id, run_id))
        return BatchEmbeddingResult(
            model=model,
            vectors=tuple(([float(index), 0.5] for index, _ in enumerate(inputs))),
            request_count=1,
            operation_id=operation_id,
            run_id=run_id,
            provider_request_id="provider-seed-1",
            cost_event_id="cost-seed-1",
            audit_event_id="audit-seed-1",
            cleanup_state="committed",
            measured_cost_usd=Decimal("0.00002"),
        )


def test_chroma_seeder_batches_one_instrumented_compatible_embedding_call(tmp_path: Path) -> None:
    tenant_id = "tenant-a"
    collection = _Collection(f"eval_shared_{tenant_slug(tenant_id)}___shared__".replace("__", "_"))
    embeddings = _EmbeddingExecutor()
    store = EvidenceStore(tmp_path / "evidence")
    seeder = InstrumentedChromaCorpusSeeder(
        collection=collection,
        embedding_executor=embeddings,
        embedding_model="openai/text-embedding-3-small",
        campaign_id="evaluation-campaign",
        store=store,
    )
    documents = (
        SyntheticChromaDocument("alpha", "fact alpha"),
        SyntheticChromaDocument("beta", "fact beta"),
        SyntheticChromaDocument("gamma", "fact gamma"),
    )

    result = seeder.seed_exactly(tenant_id, documents)

    assert len(embeddings.calls) == 1
    model, inputs, operation_id, run_id = embeddings.calls[0]
    assert model == "openai/text-embedding-3-small"
    assert inputs == tuple(f"{item.document_id}: {json.dumps(item.content)}" for item in documents)
    assert operation_id.startswith("corpus-seed:evaluation-campaign:attempt:")
    assert (
        run_id == f"control-run:evaluation-campaign:corpus-seed:{operation_id.rsplit(':', 1)[-1]}"
    )
    assert set(collection.rows) == {"alpha", "beta", "gamma"}
    assert tuple(item.sha256 for item in result) == tuple(item.sha256 for item in documents)
    assert any(event["type"] == "control.chroma-corpus.seeded" for event in store.read_events())


def test_chroma_seed_attempt_identity_changes_with_evidence_bundle(tmp_path: Path) -> None:
    tenant_id = "tenant-a"
    documents = (
        SyntheticChromaDocument("alpha", "fact alpha"),
        SyntheticChromaDocument("beta", "fact beta"),
        SyntheticChromaDocument("gamma", "fact gamma"),
    )
    observed: list[str] = []
    for name in ("first", "second"):
        executor = _EmbeddingExecutor()
        seeder = InstrumentedChromaCorpusSeeder(
            collection=_Collection(f"eval_{tenant_slug(tenant_id)}_shared"),
            embedding_executor=executor,
            embedding_model="openai/text-embedding-3-small",
            campaign_id="evaluation-campaign",
            store=EvidenceStore(tmp_path / name),
        )
        seeder.seed_exactly(tenant_id, documents)
        observed.append(executor.calls[0][2])

    assert observed[0] != observed[1]


def test_persistent_batch_embedding_executor_reserves_calls_and_commits_once() -> None:
    class Instrumentation:
        def __init__(self) -> None:
            self.reserved = []
            self.committed = []

        async def reserve_probe(self, **kwargs):
            self.reserved.append(kwargs)

        async def commit_probe(self, **kwargs):
            self.committed.append(kwargs)
            return type(
                "Evidence",
                (),
                {
                    "cost_event_id": "cost-seed",
                    "provider_request_id": kwargs["provider_request_id"],
                    "cleanup_status": kwargs["cleanup_status"],
                },
            )()

        async def mark_probe_ambiguous(self, **_kwargs):
            raise AssertionError("success must not be ambiguous")

    class Estimator:
        def estimate(self, _model, *, input_tokens, output_tokens):
            assert input_tokens > 0
            assert output_tokens == 0
            return Decimal("0.00002")

    provider_calls = []

    async def provider_call(model: str, inputs: tuple[str, ...]):
        provider_calls.append((model, inputs))
        return {
            "id": "provider-seed",
            "usage": {"prompt_tokens": 12},
            "data": [
                {"embedding": [1.0, 0.0]},
                {"embedding": [0.0, 1.0]},
            ],
        }

    instrumentation = Instrumentation()
    executor = PersistentInstrumentedBatchEmbeddingExecutor(
        instrumentation=instrumentation,
        cost_estimator=Estimator(),
        provider_call=provider_call,
        tenant_id="tenant-a",
        campaign_id="evaluation-campaign",
        max_cost_usd=Decimal("0.25"),
    )

    result = executor.embed_once(
        model="openai/text-embedding-3-small",
        inputs=("alpha", "beta"),
        operation_id="corpus-seed:evaluation-campaign",
        run_id="control-run:evaluation-campaign:corpus-seed",
    )

    assert len(instrumentation.reserved) == len(instrumentation.committed) == 1
    assert provider_calls == [("openai/text-embedding-3-small", ("alpha", "beta"))]
    assert result.vectors == ([1.0, 0.0], [0.0, 1.0])
    assert result.provider_request_id == "provider-seed"
    assert result.cost_event_id == "cost-seed"


def test_persistent_batch_embedding_accepts_litellm_hidden_provider_request_id() -> None:
    class LiteLLMEmbeddingResponse:
        _hidden_params = {
            "additional_headers": {"llm_provider-x-request-id": "provider-hidden-request"}
        }

        def model_dump(self, *, mode):
            assert mode == "json"
            return {
                "data": [{"embedding": [1.0, 0.0]}],
                "usage": {"prompt_tokens": 3, "total_tokens": 3},
                "model": "text-embedding-3-small",
                "object": "list",
            }

    vectors, request_id, input_tokens = (
        PersistentInstrumentedBatchEmbeddingExecutor._response_parts(LiteLLMEmbeddingResponse(), 1)
    )

    assert vectors == ([1.0, 0.0],)
    assert request_id == "provider-hidden-request"
    assert input_tokens == 3


def test_chroma_seeder_rejects_uninstrumented_or_malformed_batch(tmp_path: Path) -> None:
    tenant_id = "tenant-a"
    collection = _Collection(f"eval_{tenant_slug(tenant_id)}_shared")

    class BadExecutor(_EmbeddingExecutor):
        def embed_once(self, **kwargs):
            return BatchEmbeddingResult(
                model=kwargs["model"],
                vectors=([1.0],),
                request_count=0,
                operation_id=kwargs["operation_id"],
                run_id=kwargs["run_id"],
                provider_request_id="request",
                cost_event_id="cost",
                audit_event_id="audit",
                cleanup_state="committed",
                measured_cost_usd=Decimal("0"),
            )

    seeder = InstrumentedChromaCorpusSeeder(
        collection=collection,
        embedding_executor=BadExecutor(),
        embedding_model="openai/text-embedding-3-small",
        campaign_id="evaluation-campaign",
        store=EvidenceStore(tmp_path / "evidence"),
    )
    docs = tuple(SyntheticChromaDocument(item, item) for item in ("alpha", "beta", "gamma"))

    with pytest.raises(ValueError, match="exactly one instrumented"):
        seeder.seed_exactly(tenant_id, docs)
    assert set(collection.rows) == {"stale"}


@pytest.mark.parametrize("kind", ["provider", "chroma"])
def test_http_paid_probe_executor_translates_only_committed_sanitized_response(
    kind: str, tmp_path: Path
) -> None:
    authorization = _authorization(kind)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if kind == "provider":
            return httpx.Response(
                200,
                json={
                    "workflow_id": "provider-probe-workflow",
                    "verified": True,
                    "campaign_id": authorization.campaign_id,
                    "operation_id": authorization.operation_id,
                    "probes": [
                        {
                            "model": "openai/gpt-4o-mini",
                            "ok": True,
                            "latency_ms": 10,
                            "operation_id": authorization.operation_id,
                            "cost_event_id": "cost-provider",
                            "audit_event_id": "audit-provider",
                            "cost_measurement": "estimated",
                            "estimated_cost_usd": "0.00003",
                            "provider_request_id": "provider-request-provider",
                            "cleanup_status": "complete",
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "ok": True,
                "detail": None,
                "latency_ms": 8.0,
                "campaign_id": authorization.campaign_id,
                "operation_id": authorization.operation_id,
                "cost_event_id": "cost-chroma",
                "audit_event_id": "audit-chroma",
                "cost_measurement": "estimated",
                "estimated_cost_usd": "0.00001",
                "provider_request_id": "provider-request-chroma",
                "cleanup_status": "complete",
            },
        )

    store = EvidenceStore(tmp_path / "evidence")
    executor = HttpPaidProbeExecutor(
        kind=kind,  # type: ignore[arg-type]
        client=httpx.Client(
            base_url="http://127.0.0.1:7000",
            transport=httpx.MockTransport(handler),
            headers={"Authorization": "Bearer not-recorded"},
        ),
        store=store,
        signed_audit_evidence_reference=_signed_readiness_reference(store),
        provider_workflow_id="provider-probe-workflow",
        chroma_connector_ref="eval_chroma_v1",
        max_cost_usd=Decimal("0.25"),
    )

    result = executor.execute_paid_probe(authorization)

    assert result.kind == kind
    assert result.operation_id == authorization.operation_id
    assert result.run_id == authorization.run_id
    assert result.request_count == 1
    assert result.audit_chain_signed is True
    assert result.measured_cost_usd > 0
    if kind == "chroma":
        assert result.connector_request_id == result.provider_request_id
    body = json.loads(requests[0].content)
    assert body["campaign_id"] == authorization.campaign_id
    assert body["run_id"] == authorization.run_id
    assert body["run_cap_usd"] == "0.25"
    if kind == "provider":
        assert requests[0].url.path.startswith("/api/studio/v1/workflows/")
    assert "not-recorded" not in (executor.store.root / "events.ndjson").read_text()


def test_http_paid_probe_executor_rejects_missing_cost_or_unsigned_readiness(
    tmp_path: Path,
) -> None:
    authorization = _authorization("provider")
    response = {
        "workflow_id": "provider-probe-workflow",
        "verified": True,
        "campaign_id": authorization.campaign_id,
        "operation_id": authorization.operation_id,
        "probes": [
            {
                "model": "openai/gpt-4o-mini",
                "ok": True,
                "latency_ms": 10,
                "operation_id": authorization.operation_id,
                "cost_event_id": "cost-provider",
                "audit_event_id": "audit-provider",
                "cost_measurement": "measured",
                "estimated_cost_usd": None,
                "provider_request_id": "provider-request-provider",
                "cleanup_status": "complete",
            }
        ],
    }
    store = EvidenceStore(tmp_path / "evidence")
    executor = HttpPaidProbeExecutor(
        kind="provider",
        client=httpx.Client(
            base_url="http://127.0.0.1:7000",
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=response)),
        ),
        store=store,
        signed_audit_evidence_reference=_signed_readiness_reference(store),
        provider_workflow_id="provider-probe-workflow",
        chroma_connector_ref="eval_chroma_v1",
        max_cost_usd=Decimal("0.25"),
    )

    with pytest.raises(RuntimeError, match="cost amount"):
        executor.execute_paid_probe(authorization)


def test_provider_paid_probe_accepts_missing_upstream_request_id(tmp_path: Path) -> None:
    authorization = _authorization("provider")
    response = {
        "workflow_id": "provider-probe-workflow",
        "verified": True,
        "campaign_id": authorization.campaign_id,
        "operation_id": authorization.operation_id,
        "probes": [
            {
                "model": "openai/gpt-4o-mini",
                "ok": True,
                "latency_ms": 10,
                "operation_id": authorization.operation_id,
                "cost_event_id": "cost-provider",
                "audit_event_id": "audit-provider",
                "cost_measurement": "estimated",
                "estimated_cost_usd": "0.00003",
                "provider_request_id": None,
                "cleanup_status": "complete",
            }
        ],
    }
    store = EvidenceStore(tmp_path / "evidence")
    executor = HttpPaidProbeExecutor(
        kind="provider",
        client=httpx.Client(
            base_url="http://127.0.0.1:7000",
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=response)),
        ),
        store=store,
        signed_audit_evidence_reference=_signed_readiness_reference(store),
        provider_workflow_id="provider-probe-workflow",
        chroma_connector_ref="eval_chroma_v1",
        max_cost_usd=Decimal("0.25"),
    )

    result = executor.execute_paid_probe(authorization)

    assert result.provider_request_id is None
    observed = [
        event for event in store.read_events() if event["type"] == "control.http-probe.observed"
    ]
    assert observed[0]["correlation"].get("provider_request_id") is None
