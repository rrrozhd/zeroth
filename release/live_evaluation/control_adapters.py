"""Fail-closed local adapters for the live-evaluation control gate.

The adapters persist only sanitized observations. HTTP clients are supplied by
the campaign owner so authentication remains outside this module and is never
serialized into evidence. No constructor performs I/O.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import subprocess
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from time import perf_counter
from typing import Any, Literal, Protocol
from urllib.parse import urlparse

import httpx

from zeroth.integrations.memory.tenant_scoped import tenant_slug

from .control_gate import (
    ChromaCorpusDocument,
    ChromaIdentity,
    PaidProbeAuthorization,
    PaidProbeResult,
    ProbeKind,
    SignedAuditReadiness,
)
from .control_gate_runtime import SyntheticChromaDocument
from .evidence import CorrelationIds, EvidenceStore

CommandRunner = Callable[[tuple[str, ...]], tuple[int, str, str]]


def _corpus_seed_attempt_identity(campaign_id: str, store: EvidenceStore) -> tuple[str, str]:
    """Return an attempt identity scoped to one evidence bundle."""
    token = hashlib.sha256(str(store.root).encode("utf-8")).hexdigest()[:12]
    return (
        f"corpus-seed:{campaign_id}:attempt:{token}",
        f"control-run:{campaign_id}:corpus-seed:{token}",
    )


def _require_loopback_client(client: httpx.Client) -> None:
    host = urlparse(str(client.base_url)).hostname
    if host is None:
        raise ValueError("HTTP adapter requires an absolute base URL")
    try:
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError("control adapter HTTP service must be loopback-only")
    except ValueError as exc:
        if host != "localhost":
            raise ValueError("control adapter HTTP service must be loopback-only") from exc


def _object_response(response: httpx.Response, *, context: str) -> dict[str, object]:
    if response.status_code < 200 or response.status_code >= 300:
        raise RuntimeError(f"{context} returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"{context} returned malformed JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{context} returned a non-object response")
    return payload


class HttpSignedAuditInspector:
    """Inspect signed readiness without retaining request headers or messages."""

    def __init__(
        self,
        *,
        client: httpx.Client,
        store: EvidenceStore,
        signing_reference: str,
    ) -> None:
        _require_loopback_client(client)
        self.client = client
        self.store = store
        self.signing_reference = signing_reference

    def inspect(self) -> SignedAuditReadiness:
        try:
            response = self.client.get("/v1/audit-readiness")
        except httpx.HTTPError as exc:
            raise RuntimeError("signed audit readiness service is unavailable") from exc
        payload = _object_response(response, context="signed audit readiness")
        if not (
            payload.get("ready") is True
            and payload.get("state") == "signed"
            and payload.get("signer_available") is True
        ):
            raise RuntimeError("signed audit readiness was not proved")
        event_id = self.store.append_event(
            "control.audit-readiness.inspected",
            {
                "ready": True,
                "state": "signed",
                "signer_available": True,
                "deployment_mode": str(payload.get("deployment_mode", "unknown")),
                "http": {"method": "GET", "path": "/v1/audit-readiness", "status": 200},
            },
        )
        return SignedAuditReadiness(
            state="signed",
            algorithm="hmac-sha256",
            signing_reference=self.signing_reference,
            evidence_reference=f"events.ndjson#{event_id}",
        )


def _subprocess_runner(argv: tuple[str, ...]) -> tuple[int, str, str]:
    completed = subprocess.run(  # noqa: S603 - fixed executable and explicit container name
        argv,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return completed.returncode, completed.stdout, completed.stderr


class DockerChromaInspector:
    """Verify the exact running image, health, loopback mapping, and API version."""

    def __init__(
        self,
        *,
        client: httpx.Client,
        store: EvidenceStore,
        container_name: str,
        expected_image: str,
        host: str,
        port: int,
        command_runner: CommandRunner = _subprocess_runner,
    ) -> None:
        _require_loopback_client(client)
        if expected_image.endswith(":latest") or ":" not in expected_image:
            raise ValueError("expected Chroma image must be version-pinned")
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError("Chroma host must be numeric loopback")
        self.client = client
        self.store = store
        self.container_name = container_name
        self.expected_image = expected_image
        self.host = host
        self.port = port
        self.command_runner = command_runner

    def inspect(self) -> ChromaIdentity:
        code, stdout, _stderr = self.command_runner(("docker", "inspect", self.container_name))
        if code != 0:
            raise RuntimeError("Chroma container inspection failed")
        try:
            payload = json.loads(stdout)
            container = payload[0]
        except (json.JSONDecodeError, IndexError, KeyError, TypeError) as exc:
            raise RuntimeError("Chroma container inspection returned malformed data") from exc
        if not isinstance(container, dict):
            raise RuntimeError("Chroma container inspection returned malformed data")
        try:
            image = container["Config"]["Image"]
            state = container["State"]
            bindings = container["NetworkSettings"]["Ports"]["8000/tcp"]
            instance_id = container["Id"]
            name = str(container["Name"]).removeprefix("/")
        except (KeyError, TypeError) as exc:
            raise RuntimeError("Chroma container inspection omitted required identity") from exc
        expected_binding = {"HostIp": self.host, "HostPort": str(self.port)}
        health = state.get("Health") if isinstance(state, dict) else None
        healthy = (
            isinstance(state, dict)
            and state.get("Running") is True
            and (health is None or isinstance(health, dict) and health.get("Status") == "healthy")
        )
        if (
            image != self.expected_image
            or name != self.container_name
            or not healthy
            or not isinstance(bindings, list)
            or expected_binding not in bindings
            or not isinstance(instance_id, str)
            or not instance_id
        ):
            raise RuntimeError("Chroma container identity, health, or loopback mapping is invalid")
        try:
            version_response = self.client.get("/api/v2/version")
        except httpx.HTTPError as exc:
            raise RuntimeError("Chroma container API is unavailable") from exc
        if version_response.status_code != 200:
            raise RuntimeError(f"Chroma container API returned HTTP {version_response.status_code}")
        try:
            version_payload = version_response.json()
        except (json.JSONDecodeError, ValueError):
            version_payload = version_response.text.strip().strip('"')
        version = (
            version_payload.get("version") if isinstance(version_payload, dict) else version_payload
        )
        if not isinstance(version, str) or not version:
            raise RuntimeError("Chroma container API omitted its protocol version")
        event_id = self.store.append_event(
            "control.chroma-container.inspected",
            {
                "container_name": self.container_name,
                "instance_id": instance_id,
                "image": image,
                "host": self.host,
                "port": self.port,
                "api_version": version,
                "running": True,
                "health": "docker-healthy" if health is not None else "api-responsive",
                "http": {"method": "GET", "path": "/api/v2/version", "status": 200},
            },
        )
        return ChromaIdentity(
            image=image,
            host=self.host,
            port=self.port,
            instance_id=instance_id,
            api_version=version,
            evidence_reference=f"events.ndjson#{event_id}",
        )


@dataclass(frozen=True, slots=True)
class BatchEmbeddingResult:
    """Vectors plus the durable accounting identities for one batch call."""

    model: str
    vectors: tuple[Sequence[float], ...]
    request_count: int
    operation_id: str
    run_id: str
    provider_request_id: str
    cost_event_id: str
    audit_event_id: str
    cleanup_state: Literal["committed"]
    measured_cost_usd: Decimal


class InstrumentedBatchEmbeddingExecutor(Protocol):
    def embed_once(
        self,
        *,
        model: str,
        inputs: tuple[str, ...],
        operation_id: str,
        run_id: str,
    ) -> BatchEmbeddingResult: ...


class PersistentInstrumentedBatchEmbeddingExecutor:
    """One batch provider call behind persistent reserve/commit instrumentation.

    The provider callable is injected and is the only object allowed to know how
    credentials are resolved. This adapter supplies neither a raw key nor an
    environment fallback.
    """

    def __init__(
        self,
        *,
        instrumentation: Any,
        cost_estimator: Any,
        provider_call: Callable[[str, tuple[str, ...]], Awaitable[Any]],
        tenant_id: str,
        campaign_id: str,
        max_cost_usd: Decimal,
    ) -> None:
        if not tenant_id or not campaign_id:
            raise ValueError("batch embedding requires tenant and campaign identity")
        if max_cost_usd <= 0 or max_cost_usd > Decimal("0.25"):
            raise ValueError("batch embedding maximum must fit the $0.25 run ceiling")
        self.instrumentation = instrumentation
        self.cost_estimator = cost_estimator
        self.provider_call = provider_call
        self.tenant_id = tenant_id
        self.campaign_id = campaign_id
        self.max_cost_usd = max_cost_usd

    @staticmethod
    def _mapping(value: object) -> Mapping[str, Any] | None:
        if isinstance(value, Mapping):
            return value
        dump = getattr(value, "model_dump", None)
        if callable(dump):
            result = dump(mode="json")
            return result if isinstance(result, Mapping) else None
        return None

    @classmethod
    def _provider_request_id(
        cls, response: object, payload: Mapping[str, Any] | None
    ) -> str | None:
        """Read provider identity from public fields or LiteLLM response headers."""
        candidates: list[object] = []
        if payload is not None:
            candidates.extend((payload.get("id"), payload.get("request_id")))
        candidates.extend((getattr(response, "id", None), getattr(response, "request_id", None)))

        hidden = cls._mapping(getattr(response, "_hidden_params", None))
        if hidden is not None:
            candidates.extend((hidden.get("id"), hidden.get("request_id")))
            for header_container in ("additional_headers", "headers"):
                headers = cls._mapping(hidden.get(header_container))
                if headers is None:
                    continue
                for key, value in headers.items():
                    normalized = str(key).lower()
                    if normalized in {
                        "x-request-id",
                        "request-id",
                        "openai-request-id",
                        "llm_provider-x-request-id",
                    }:
                        candidates.append(value)
        return next(
            (candidate for candidate in candidates if isinstance(candidate, str) and candidate),
            None,
        )

    @classmethod
    def _response_parts(
        cls, response: object, expected_count: int
    ) -> tuple[tuple[list[float], ...], str, int]:
        payload = cls._mapping(response)
        data = payload.get("data") if payload is not None else getattr(response, "data", None)
        request_id = cls._provider_request_id(response, payload)
        usage_value = (
            payload.get("usage") if payload is not None else getattr(response, "usage", None)
        )
        usage = cls._mapping(usage_value)
        input_tokens: object | None = None
        if usage is not None:
            for key in ("input_tokens", "prompt_tokens", "total_tokens"):
                candidate = usage.get(key)
                if isinstance(candidate, int) and not isinstance(candidate, bool):
                    input_tokens = candidate
                    break
        if (
            not isinstance(data, Sequence)
            or isinstance(data, (str, bytes))
            or len(data) != expected_count
            or not isinstance(request_id, str)
            or not request_id
            or not isinstance(input_tokens, int)
            or input_tokens < 0
        ):
            raise RuntimeError("embedding provider response lacks vectors, usage, or request ID")
        vectors: list[list[float]] = []
        dimension: int | None = None
        for item in data:
            entry = cls._mapping(item)
            vector = entry.get("embedding") if entry is not None else None
            if (
                not isinstance(vector, Sequence)
                or isinstance(vector, (str, bytes))
                or not vector
                or any(
                    isinstance(value, bool) or not isinstance(value, (int, float))
                    for value in vector
                )
            ):
                raise RuntimeError("embedding provider returned a malformed vector")
            if dimension is None:
                dimension = len(vector)
            elif len(vector) != dimension:
                raise RuntimeError("embedding provider returned inconsistent dimensions")
            vectors.append([float(value) for value in vector])
        return tuple(vectors), request_id, input_tokens

    async def _execute(
        self, *, model: str, inputs: tuple[str, ...], operation_id: str, run_id: str
    ) -> BatchEmbeddingResult:
        if not model or not inputs or not operation_id or not run_id:
            raise ValueError("batch embedding identities and inputs are required")
        estimated_max = self.cost_estimator.estimate(
            model,
            input_tokens=sum(len(item.encode("utf-8")) for item in inputs),
            output_tokens=0,
        )
        maximum = Decimal(str(estimated_max))
        if not maximum.is_finite() or maximum <= 0 or maximum > self.max_cost_usd:
            raise RuntimeError("batch embedding maximum is unknown or exceeds its ceiling")
        await self.instrumentation.reserve_probe(
            tenant_id=self.tenant_id,
            campaign_id=self.campaign_id,
            operation_id=operation_id,
            run_id=run_id,
            max_cost_usd=str(maximum),
            run_cap_usd="0.25",
            capability_id="control.corpus-seed.embedding",
            implementation_id=model,
        )
        started = perf_counter()
        try:
            response = await self.provider_call(model, inputs)
            vectors, provider_request_id, input_tokens = self._response_parts(response, len(inputs))
            actual = Decimal(
                str(self.cost_estimator.estimate(model, input_tokens=input_tokens, output_tokens=0))
            )
            if not actual.is_finite() or actual < 0 or actual > maximum:
                raise RuntimeError("batch embedding actual cost is invalid")
        except BaseException as exc:
            await self.instrumentation.mark_probe_ambiguous(
                tenant_id=self.tenant_id,
                campaign_id=self.campaign_id,
                operation_id=operation_id,
                run_id=run_id,
                capability_id="control.corpus-seed.embedding",
                implementation_id=model,
                latency_ms=int((perf_counter() - started) * 1000),
                cleanup_status="pending_reconciliation:embedding_seed",
            )
            raise RuntimeError("batch embedding outcome is ambiguous") from exc
        evidence = await self.instrumentation.commit_probe(
            tenant_id=self.tenant_id,
            campaign_id=self.campaign_id,
            operation_id=operation_id,
            run_id=run_id,
            capability_id="control.corpus-seed.embedding",
            implementation_id=model,
            actual_cost_usd=str(actual),
            cost_measurement="estimated",
            provider_request_id=provider_request_id,
            cleanup_status="complete",
            latency_ms=int((perf_counter() - started) * 1000),
            input_tokens=input_tokens,
            output_tokens=0,
        )
        if (
            getattr(evidence, "cleanup_status", None) != "complete"
            or getattr(evidence, "provider_request_id", None) != provider_request_id
            or not getattr(evidence, "cost_event_id", None)
        ):
            raise RuntimeError("batch embedding settlement evidence is incomplete")
        cost_event_id = str(evidence.cost_event_id)
        return BatchEmbeddingResult(
            model=model,
            vectors=vectors,
            request_count=1,
            operation_id=operation_id,
            run_id=run_id,
            provider_request_id=provider_request_id,
            cost_event_id=cost_event_id,
            audit_event_id=f"audit_{cost_event_id}",
            cleanup_state="committed",
            measured_cost_usd=actual,
        )

    def embed_once(
        self,
        *,
        model: str,
        inputs: tuple[str, ...],
        operation_id: str,
        run_id: str,
    ) -> BatchEmbeddingResult:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self._execute(
                    model=model,
                    inputs=inputs,
                    operation_id=operation_id,
                    run_id=run_id,
                )
            )
        raise RuntimeError("synchronous corpus seeding cannot run inside an event loop")

    async def embed_once_async(
        self,
        *,
        model: str,
        inputs: tuple[str, ...],
        operation_id: str,
        run_id: str,
    ) -> BatchEmbeddingResult:
        """Execute the same guarded batch boundary from an async service route."""
        return await self._execute(
            model=model,
            inputs=inputs,
            operation_id=operation_id,
            run_id=run_id,
        )


class InstrumentedChromaCorpusSeeder:
    """Atomically replace one tenant collection after one accounted batch embed."""

    def __init__(
        self,
        *,
        collection: object,
        embedding_executor: InstrumentedBatchEmbeddingExecutor,
        embedding_model: str,
        campaign_id: str,
        store: EvidenceStore,
    ) -> None:
        self.collection = collection
        self.embedding_executor = embedding_executor
        self.embedding_model = embedding_model
        self.campaign_id = campaign_id
        self.store = store

    def seed_exactly(
        self,
        tenant_id: str,
        documents: tuple[SyntheticChromaDocument, ...],
    ) -> tuple[ChromaCorpusDocument, ...]:
        if len(documents) != 3 or len({item.document_id for item in documents}) != 3:
            raise ValueError("corpus seeding requires exactly three unique documents")
        collection_name = getattr(self.collection, "name", None)
        tenant_hash = tenant_slug(tenant_id).rsplit("-", 1)[-1]
        if not isinstance(collection_name, str) or tenant_hash not in collection_name:
            raise ValueError("Chroma collection does not match the tenant namespace")
        inputs = tuple(f"{item.document_id}: {json.dumps(item.content)}" for item in documents)
        operation_id, run_id = _corpus_seed_attempt_identity(self.campaign_id, self.store)
        result = self.embedding_executor.embed_once(
            model=self.embedding_model,
            inputs=inputs,
            operation_id=operation_id,
            run_id=run_id,
        )
        identities = (
            result.provider_request_id,
            result.cost_event_id,
            result.audit_event_id,
        )
        if (
            result.request_count != 1
            or result.cleanup_state != "committed"
            or result.model != self.embedding_model
            or result.operation_id != operation_id
            or result.run_id != run_id
            or not all(identities)
            or len(set(identities)) != 3
            or len(result.vectors) != len(documents)
            or any(
                not vector
                or any(
                    isinstance(value, bool) or not isinstance(value, (int, float))
                    for value in vector
                )
                for vector in result.vectors
            )
            or result.measured_cost_usd < 0
        ):
            raise ValueError("corpus seeding requires exactly one instrumented embedding batch")

        getter = getattr(self.collection, "get", None)
        deleter = getattr(self.collection, "delete", None)
        upsert = getattr(self.collection, "upsert", None)
        if not all(callable(item) for item in (getter, deleter, upsert)):
            raise TypeError("Chroma collection must implement get, delete, and upsert")
        before = getter(include=["documents", "metadatas", "embeddings"])
        existing_ids = before.get("ids") if isinstance(before, dict) else None
        if not isinstance(existing_ids, list):
            raise RuntimeError("Chroma collection returned malformed existing IDs")
        expected_ids = [item.document_id for item in documents]
        stale = [item for item in existing_ids if item not in set(expected_ids)]
        if stale:
            deleter(ids=stale)
        upsert(
            ids=expected_ids,
            documents=[json.dumps(item.content) for item in documents],
            embeddings=[list(vector) for vector in result.vectors],
            metadatas=[
                {"key": item.document_id, "scope": "shared", "target": "__shared__"}
                for item in documents
            ],
        )
        after = getter(include=["documents", "metadatas", "embeddings"])
        after_ids = after.get("ids") if isinstance(after, dict) else None
        if not isinstance(after_ids, list) or set(after_ids) != set(expected_ids):
            raise RuntimeError("Chroma corpus did not converge to the exact expected IDs")
        event_id = self.store.append_event(
            "control.chroma-corpus.seeded",
            {
                "tenant_id": tenant_id,
                "collection": collection_name,
                "document_ids": expected_ids,
                "document_hashes": [item.sha256 for item in documents],
                "embedding_model": self.embedding_model,
                "request_count": 1,
                "measured_cost_usd": str(result.measured_cost_usd),
            },
            correlation=CorrelationIds(
                operation_id=operation_id,
                run_id=run_id,
                audit_event_id=result.audit_event_id,
                cost_event_id=result.cost_event_id,
                provider_request_id=result.provider_request_id,
            ),
        )
        # The gate records its own readiness event; this event proves the paid
        # seed boundary and exact postcondition without storing vectors.
        _ = event_id
        return tuple(
            ChromaCorpusDocument(item.document_id, tenant_id, item.sha256) for item in documents
        )


class HttpInstrumentedChromaCorpusSeeder:
    """Delegate corpus seeding to the signed local service instrumentation plane."""

    def __init__(
        self,
        *,
        client: httpx.Client,
        store: EvidenceStore,
        campaign_id: str,
        connector_ref: str,
        embedding_model: str,
        max_cost_usd: Decimal,
    ) -> None:
        _require_loopback_client(client)
        if max_cost_usd <= 0 or max_cost_usd > Decimal("0.25"):
            raise ValueError("corpus seed maximum must fit the $0.25 run ceiling")
        self.client = client
        self.store = store
        self.campaign_id = campaign_id
        self.connector_ref = connector_ref
        self.embedding_model = embedding_model
        self.max_cost_usd = max_cost_usd

    def seed_exactly(
        self,
        tenant_id: str,
        documents: tuple[SyntheticChromaDocument, ...],
    ) -> tuple[ChromaCorpusDocument, ...]:
        if len(documents) != 3 or len({item.document_id for item in documents}) != 3:
            raise ValueError("corpus seeding requires exactly three unique documents")
        operation_id, run_id = _corpus_seed_attempt_identity(self.campaign_id, self.store)
        path = "/v1/evaluation/control/chroma-corpus/seed"
        body = {
            "campaign_id": self.campaign_id,
            "tenant_id": tenant_id,
            "connector_ref": self.connector_ref,
            "embedding_model": self.embedding_model,
            "operation_id": operation_id,
            "run_id": run_id,
            "max_cost_usd": str(self.max_cost_usd),
            "run_cap_usd": "0.25",
            "documents": [
                {
                    "document_id": item.document_id,
                    "content": item.content,
                    "sha256": item.sha256,
                }
                for item in documents
            ],
        }
        try:
            response = self.client.post(path, json=body)
        except httpx.HTTPError as exc:
            raise RuntimeError("instrumented corpus seed service is unavailable") from exc
        payload = _object_response(response, context="instrumented corpus seed")
        expected_documents = [
            {
                "document_id": item.document_id,
                "tenant_id": tenant_id,
                "sha256": item.sha256,
            }
            for item in documents
        ]
        if (
            payload.get("campaign_id") != self.campaign_id
            or payload.get("operation_id") != operation_id
            or payload.get("run_id") != run_id
            or payload.get("documents") != expected_documents
            or payload.get("request_count") != 1
            or payload.get("cleanup_status") != "complete"
        ):
            raise RuntimeError("instrumented corpus seed failed identity or postcondition checks")
        identities = (
            payload.get("audit_event_id"),
            payload.get("cost_event_id"),
            payload.get("provider_request_id"),
        )
        if not all(isinstance(item, str) and item for item in identities):
            raise RuntimeError("instrumented corpus seed omitted accounting identities")
        event_id = self.store.append_event(
            "control.http-corpus-seed.observed",
            {
                "document_count": 3,
                "embedding_model": self.embedding_model,
                "http": {"method": "POST", "path": path, "status": response.status_code},
                "request_count": 1,
                "cleanup_state": "committed",
            },
            correlation=CorrelationIds(
                operation_id=operation_id,
                run_id=run_id,
                audit_event_id=str(identities[0]),
                cost_event_id=str(identities[1]),
                provider_request_id=str(identities[2]),
            ),
        )
        _ = event_id
        return tuple(
            ChromaCorpusDocument(item.document_id, tenant_id, item.sha256) for item in documents
        )


class HttpPaidProbeExecutor:
    """Translate one instrumented service response into a sanitized probe result."""

    def __init__(
        self,
        *,
        kind: ProbeKind,
        client: httpx.Client,
        store: EvidenceStore,
        signed_audit_evidence_reference: str,
        provider_workflow_id: str,
        chroma_connector_ref: str,
        max_cost_usd: Decimal,
    ) -> None:
        _require_loopback_client(client)
        if kind not in {"provider", "chroma"}:
            raise ValueError("unsupported paid probe kind")
        if max_cost_usd <= 0 or max_cost_usd > Decimal("0.25"):
            raise ValueError("probe maximum must fit the $0.25 run ceiling")
        self.kind = kind
        self.client = client
        self.store = store
        self.provider_workflow_id = provider_workflow_id
        self.chroma_connector_ref = chroma_connector_ref
        self.max_cost_usd = max_cost_usd
        self.signed_audit_evidence_reference = signed_audit_evidence_reference
        self._validate_signed_readiness_reference()

    def _validate_signed_readiness_reference(self) -> None:
        prefix = "events.ndjson#"
        if not self.signed_audit_evidence_reference.startswith(prefix):
            raise ValueError("paid probe requires signed-audit evidence")
        event_id = self.signed_audit_evidence_reference.removeprefix(prefix)
        matching = [
            event for event in self.store.read_events() if event.get("event_id") == event_id
        ]
        if len(matching) != 1:
            raise ValueError("paid probe signed-audit evidence does not exist")
        event = matching[0]
        data = event.get("data")
        if (
            event.get("type") != "control.audit-readiness.inspected"
            or not isinstance(data, dict)
            or data.get("ready") is not True
            or data.get("state") != "signed"
            or data.get("signer_available") is not True
        ):
            raise ValueError("paid probe requires signed-audit evidence")

    def execute_paid_probe(self, authorization: PaidProbeAuthorization) -> PaidProbeResult:
        if authorization.kind != self.kind:
            raise ValueError("probe authorization kind does not match the executor")
        body = {
            "campaign_id": authorization.campaign_id,
            "operation_id": authorization.operation_id,
            "run_id": authorization.run_id,
            "max_cost_usd": str(self.max_cost_usd),
            "run_cap_usd": "0.25",
        }
        if self.kind == "provider":
            body.update(
                {
                    "acknowledge_external_call": True,
                    "timeout_seconds": 15,
                    "max_models": 1,
                }
            )
            path = f"/api/studio/v1/workflows/{self.provider_workflow_id}/verify-provider"
        else:
            path = f"/v1/connectors/{self.chroma_connector_ref}/test"
        try:
            response = self.client.post(path, json=body)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"{self.kind} paid probe service is unavailable") from exc
        payload = _object_response(response, context=f"{self.kind} paid probe")
        if self.kind == "provider":
            if (
                payload.get("verified") is not True
                or payload.get("campaign_id") != authorization.campaign_id
                or payload.get("operation_id") != authorization.operation_id
                or not isinstance(payload.get("probes"), list)
                or len(payload["probes"]) != 1
                or not isinstance(payload["probes"][0], dict)
            ):
                raise RuntimeError("provider paid probe response failed identity or count checks")
            observation = payload["probes"][0]
            if observation.get("ok") is not True:
                raise RuntimeError("provider paid probe did not succeed")
            if observation.get("operation_id") != authorization.operation_id:
                raise RuntimeError("provider paid probe operation identity is invalid")
            connector_request_id = None
        else:
            if (
                payload.get("ok") is not True
                or payload.get("campaign_id") != authorization.campaign_id
                or payload.get("operation_id") != authorization.operation_id
            ):
                raise RuntimeError("Chroma paid probe response failed identity checks")
            observation = payload
            # The embedding provider request is the authoritative external
            # request made by this connector round-trip. The connector API does
            # not mint a second request identity.
            connector_request_id = observation.get("provider_request_id")

        if observation.get("cleanup_status") != "complete":
            raise RuntimeError("paid probe reservation was not committed and cleaned up")
        cost_measurement = observation.get("cost_measurement")
        raw_cost = observation.get("estimated_cost_usd")
        if cost_measurement != "estimated" or raw_cost is None:
            # The current API omits the amount when it labels cost "measured".
            # Accepting that response would fabricate economics evidence.
            raise RuntimeError("paid probe response does not expose its cost amount")
        try:
            cost = Decimal(str(raw_cost))
        except InvalidOperation as exc:
            raise RuntimeError("paid probe response has an invalid cost amount") from exc
        if cost < 0 or cost > self.max_cost_usd:
            raise RuntimeError("paid probe cost is outside its admitted maximum")
        cost_event_id = observation.get("cost_event_id")
        audit_event_id = observation.get("audit_event_id")
        provider_request_id = observation.get("provider_request_id")
        if not all(isinstance(item, str) and item for item in (cost_event_id, audit_event_id)):
            raise RuntimeError("paid probe response omitted an evidence identity")
        if provider_request_id is not None and not (
            isinstance(provider_request_id, str) and provider_request_id
        ):
            raise RuntimeError("paid probe response has an invalid provider request identity")
        if self.kind == "chroma" and provider_request_id is None:
            raise RuntimeError("Chroma paid probe omitted its provider request identity")
        event_id = self.store.append_event(
            "control.http-probe.observed",
            {
                "kind": self.kind,
                "http": {"method": "POST", "path": path, "status": response.status_code},
                "request_count": 1,
                "cost_measurement": "estimated",
                "cost_usd": str(cost),
                "cleanup_state": "committed",
                "signed_audit_evidence_reference": self.signed_audit_evidence_reference,
            },
            correlation=CorrelationIds(
                operation_id=authorization.operation_id,
                run_id=authorization.run_id,
                audit_event_id=audit_event_id,
                cost_event_id=cost_event_id,
                provider_request_id=provider_request_id,
            ),
        )
        _ = event_id
        return PaidProbeResult(
            kind=self.kind,
            operation_id=authorization.operation_id,
            run_id=authorization.run_id,
            audit_event_id=audit_event_id,
            cost_event_id=cost_event_id,
            provider_request_id=provider_request_id,
            connector_request_id=connector_request_id,
            request_count=1,
            cache_hit=False,
            audit_chain_signed=True,
            cleanup_state="committed",
            measured_cost_usd=cost,
        )
