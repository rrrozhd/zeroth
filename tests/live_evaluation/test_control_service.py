from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

from fastapi import APIRouter, FastAPI, Request
from fastapi.testclient import TestClient

from release.live_evaluation.config import CampaignConfig
from release.live_evaluation.control_service import (
    _secret_aware_embedding_call,
    register_control_corpus_routes,
)
from zeroth.governance.identity import AuthMethod, AuthenticatedPrincipal, ServiceRole
from zeroth.service.api.authorization import Permission
from zeroth.service.api.route_authorization import permission_for_route_name


def test_control_seed_route_is_declared_in_fail_closed_authorization_inventory() -> None:
    assert permission_for_route_name("seed_control_corpus") is Permission.CONNECTOR_ADMIN


def test_campaign_fault_route_uses_dedicated_evaluation_administration() -> None:
    assert permission_for_route_name("arm_fault") is Permission.EVALUATION_ADMIN


async def test_control_embedding_uses_campaign_secret_provider(monkeypatch) -> None:
    calls: list[tuple[str, str | None]] = []

    class SecretProvider:
        def resolve_secret(
            self,
            name: str,
            *,
            tenant_id: str | None = None,
            deployment_ref: str | None = None,
        ) -> str | None:
            assert deployment_ref is None
            calls.append((name, tenant_id))
            return "provider-key-from-secret-store"

    observed: dict[str, object] = {}

    async def fake_embedding(**kwargs):
        observed.update(kwargs)
        return {"id": "embedding-request", "data": [], "usage": {"input_tokens": 1}}

    monkeypatch.setattr("litellm.aembedding", fake_embedding)

    await _secret_aware_embedding_call(
        secret_provider=SecretProvider(),
        secret_ref="llm.openai",
        tenant_id="tenant-a",
        model="openai/text-embedding-3-small",
        inputs=("alpha", "beta"),
    )

    assert calls == [("llm.openai", "tenant-a")]
    assert observed == {
        "model": "openai/text-embedding-3-small",
        "input": ["alpha", "beta"],
        "api_key": "provider-key-from-secret-store",
    }


def _campaign(tmp_path: Path) -> CampaignConfig:
    return CampaignConfig.model_validate(
        {
            "schema_version": 1,
            "campaign_id": "evaluation-control-service",
            "tenant_id": "evaluation-control-service",
            "provider": "openai",
            "model": "openai/gpt-4o-mini",
            "embedding_model": "openai/text-embedding-3-small",
            "vector_backend": "chroma",
            "campaign_budget_usd": "10.00",
            "per_run_cap_usd": "0.25",
            "provider_secret_ref": "llm.openai",
            "artifact_root": str(tmp_path / "external"),
            "action_sink_root": str(tmp_path / "external" / "sink"),
        }
    )


def test_control_seed_route_rejects_noncanonical_fixture_before_provider_boundary(
    tmp_path: Path,
) -> None:
    campaign = _campaign(tmp_path)
    app = FastAPI()
    app.state.bootstrap = SimpleNamespace(
        evaluation_campaign=campaign,
        deployment=SimpleNamespace(
            tenant_id=campaign.tenant_id,
            workspace_id=None,
        ),
        audit_repository=None,
        probe_instrumentation=SimpleNamespace(
            reserve_probe=lambda **kwargs: (_ for _ in ()).throw(
                AssertionError("provider boundary must not be reached")
            )
        ),
    )
    principal = AuthenticatedPrincipal(
        subject="evaluation-control-service:admin",
        auth_method=AuthMethod.API_KEY,
        roles=[ServiceRole.ADMIN],
        tenant_id=campaign.tenant_id,
        workspace_id=None,
    )

    @app.middleware("http")
    async def inject_principal(request: Request, call_next):
        request.state.principal = principal
        return await call_next(request)

    router = APIRouter(prefix="/v1")
    register_control_corpus_routes(router)
    app.include_router(router)
    content = "not the fixed evaluation fixture"
    response = TestClient(app).post(
        "/v1/evaluation/control/chroma-corpus/seed",
        json={
            "campaign_id": campaign.campaign_id,
            "tenant_id": campaign.tenant_id,
            "connector_ref": "evaluation-chroma",
            "embedding_model": campaign.embedding_model,
            "operation_id": f"corpus-seed:{campaign.campaign_id}:attempt:0123456789ab",
            "run_id": f"control-run:{campaign.campaign_id}:corpus-seed:0123456789ab",
            "max_cost_usd": "0.25",
            "run_cap_usd": "0.25",
            "documents": [
                {
                    "document_id": f"wrong-{index}",
                    "content": content,
                    "sha256": f"sha256:{hashlib.sha256(content.encode()).hexdigest()}",
                }
                for index in range(3)
            ],
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "corpus differs from the fixed evaluation fixture"
