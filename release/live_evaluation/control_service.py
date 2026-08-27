"""Signed, evaluation-only HTTP boundary for one instrumented corpus seed."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from decimal import Decimal
from time import perf_counter

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from zeroth.integrations.memory.governed.models import MemoryScope
from zeroth.integrations.memory.tenant_scoped import tenant_slug
from zeroth.platform.secrets import resolve_secret_async
from zeroth.service.api.authorization import Permission, require_permission

from .control_adapters import PersistentInstrumentedBatchEmbeddingExecutor
from .control_gate_runtime import _SYNTHETIC_DOCUMENTS


async def _secret_aware_embedding_call(
    *,
    secret_provider: object,
    secret_ref: str,
    tenant_id: str,
    model: str,
    inputs: tuple[str, ...],
) -> object:
    """Invoke one embedding batch with the campaign's tenant-scoped credential."""
    api_key = await resolve_secret_async(secret_provider, secret_ref, tenant_id=tenant_id)
    if not api_key:
        raise RuntimeError("campaign embedding credential is unavailable")
    import litellm

    return await litellm.aembedding(model=model, input=list(inputs), api_key=api_key)


class CorpusSeedDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=4096)
    sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class CorpusSeedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    campaign_id: str
    tenant_id: str
    connector_ref: str
    embedding_model: str
    operation_id: str
    run_id: str
    max_cost_usd: Decimal = Field(gt=0, le=Decimal("0.25"))
    run_cap_usd: Decimal
    documents: tuple[CorpusSeedDocument, ...]


def _unwrap_chroma(connector: object) -> object:
    current = connector
    seen: set[int] = set()
    while id(current) not in seen:
        seen.add(id(current))
        if callable(getattr(current, "_get_collection", None)):
            return current
        nested = getattr(current, "_inner", None) or getattr(current, "_connector", None)
        if nested is None:
            break
        current = nested
    raise RuntimeError("evaluation corpus connector is not a Chroma connector")


def _valid_seed_identity(campaign_id: str, operation_id: str, run_id: str) -> bool:
    match = re.fullmatch(
        rf"corpus-seed:{re.escape(campaign_id)}:attempt:([0-9a-f]{{12}})",
        operation_id,
    )
    return bool(
        match
        and run_id
        == f"control-run:{campaign_id}:corpus-seed:{match.group(1)}"
    )


def register_control_corpus_routes(router: APIRouter) -> None:
    @router.post("/evaluation/control/chroma-corpus/seed")
    async def seed_control_corpus(body: CorpusSeedRequest, request: Request) -> dict[str, object]:
        await require_permission(request, Permission.CONNECTOR_ADMIN)
        bootstrap = request.app.state.bootstrap
        campaign = getattr(bootstrap, "evaluation_campaign", None)
        deployment = getattr(bootstrap, "deployment", None)
        if (
            campaign is None
            or body.campaign_id != campaign.campaign_id
            or body.tenant_id != campaign.tenant_id
            or getattr(deployment, "tenant_id", None) != campaign.tenant_id
            or body.embedding_model != campaign.embedding_model
            or not _valid_seed_identity(
                campaign.campaign_id, body.operation_id, body.run_id
            )
            or body.run_cap_usd != Decimal("0.25")
            or len(body.documents) != 3
            or len({item.document_id for item in body.documents}) != 3
        ):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid campaign seed")
        for item in body.documents:
            digest = f"sha256:{hashlib.sha256(item.content.encode()).hexdigest()}"
            if item.sha256 != digest:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid document hash")
        expected_documents = {
            item.document_id: (item.content, item.sha256) for item in _SYNTHETIC_DOCUMENTS
        }
        observed_documents = {
            item.document_id: (item.content, item.sha256) for item in body.documents
        }
        if observed_documents != expected_documents:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "corpus differs from the fixed evaluation fixture",
            )

        instrumentation = getattr(bootstrap, "probe_instrumentation", None)
        estimator = getattr(bootstrap, "cost_estimator", None)
        resolver = getattr(bootstrap, "memory_resolver", None)
        if instrumentation is None or estimator is None or resolver is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "corpus instrumentation control plane unavailable",
            )
        try:
            bindings = await resolver.resolve(
                [body.connector_ref],
                runtime_context={"tenant_id": body.tenant_id},
                effective_capabilities=None,
            )
            raw = _unwrap_chroma(bindings[0].connector)
            target = f"{tenant_slug(body.tenant_id)}____shared__"
            collection = await raw._get_collection(MemoryScope.SHARED, target)
        except Exception as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "Chroma corpus target unavailable"
            ) from exc

        secret_provider = getattr(bootstrap, "secret_provider", None)
        if secret_provider is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "campaign secret provider unavailable",
            )
        executor = PersistentInstrumentedBatchEmbeddingExecutor(
            instrumentation=instrumentation,
            cost_estimator=estimator,
            provider_call=lambda model, inputs: _secret_aware_embedding_call(
                secret_provider=secret_provider,
                secret_ref=campaign.provider_secret_ref,
                tenant_id=body.tenant_id,
                model=model,
                inputs=inputs,
            ),
            tenant_id=body.tenant_id,
            campaign_id=body.campaign_id,
            max_cost_usd=body.max_cost_usd,
        )
        inputs = tuple(f"{item.document_id}: {json.dumps(item.content)}" for item in body.documents)
        started = perf_counter()
        try:
            result = await executor.embed_once_async(
                model=body.embedding_model,
                inputs=inputs,
                operation_id=body.operation_id,
                run_id=body.run_id,
            )
            ids = [item.document_id for item in body.documents]
            existing = await asyncio.to_thread(
                collection.get, include=["documents", "metadatas", "embeddings"]
            )
            stale = [item for item in existing.get("ids", []) if item not in set(ids)]
            if stale:
                await asyncio.to_thread(collection.delete, ids=stale)
            await asyncio.to_thread(
                collection.upsert,
                ids=ids,
                documents=[json.dumps(item.content) for item in body.documents],
                embeddings=[list(vector) for vector in result.vectors],
                metadatas=[
                    {"key": item.document_id, "scope": "shared", "target": "__shared__"}
                    for item in body.documents
                ],
            )
            observed = await asyncio.to_thread(collection.get, include=["metadatas"])
            if set(observed.get("ids", [])) != set(ids):
                raise RuntimeError("corpus postcondition failed")
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "instrumented corpus seed failed; reservation may require reconciliation",
            ) from exc
        return {
            "campaign_id": body.campaign_id,
            "operation_id": body.operation_id,
            "run_id": body.run_id,
            "request_count": 1,
            "cleanup_status": "complete",
            "latency_ms": int((perf_counter() - started) * 1000),
            "audit_event_id": result.audit_event_id,
            "cost_event_id": result.cost_event_id,
            "provider_request_id": result.provider_request_id,
            "documents": [
                {
                    "document_id": item.document_id,
                    "tenant_id": body.tenant_id,
                    "sha256": item.sha256,
                }
                for item in body.documents
            ],
        }
