"""Tests for the model right-sizing REST API endpoints."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from zeroth.governance.identity import AuthenticatedPrincipal, AuthMethod, ServiceRole
from zeroth.core.service.rightsizing_api import register_rightsizing_routes


def _make_app(*, roles: list[ServiceRole] | None = None, bootstrap: object | None = None) -> FastAPI:
    """Minimal app with right-sizing routes and an injected principal (defaults ADMIN)."""
    app = FastAPI()
    if bootstrap is not None:
        app.state.bootstrap = bootstrap
    principal = AuthenticatedPrincipal(
        subject="test",
        auth_method=AuthMethod.API_KEY,
        roles=roles if roles is not None else [ServiceRole.ADMIN],
    )

    @app.middleware("http")
    async def _inject_principal(request, call_next):
        request.state.principal = principal
        return await call_next(request)

    router = APIRouter(prefix="/v1")
    register_rightsizing_routes(router)
    app.include_router(router)
    return app


def _bootstrap_with_audits(records: list | None = None, runs: list | None = None):
    """A fake bootstrap whose audit_repository.list returns ``records`` (+ optional runs)."""

    async def _list(query):
        return list(records or [])

    async def _list_runs(deployment_ref, *, status=None, limit=50, offset=0):
        return list(runs or [])

    return SimpleNamespace(
        deployment=SimpleNamespace(deployment_ref="default", tenant_id="default", workspace_id=None),
        audit_repository=SimpleNamespace(list=_list),
        run_repository=SimpleNamespace(list_runs=_list_runs),
    )


def test_returns_cheaper_candidates_for_known_model() -> None:
    client = TestClient(_make_app())
    resp = client.post("/v1/econ/rightsizing", json={"incumbent": "gpt-4o", "limit": 5})
    assert resp.status_code == 200
    body = resp.json()
    assert body["incumbent_known"] is True
    assert body["incumbent"] == "gpt-4o"
    assert len(body["candidates"]) >= 1
    for c in body["candidates"]:
        assert c["blended_per_mtok_usd"] < body["incumbent_blended_per_mtok_usd"]


def test_unknown_model_is_200_with_flag() -> None:
    client = TestClient(_make_app())
    resp = client.post("/v1/econ/rightsizing", json={"incumbent": "totally-made-up-model"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["incumbent_known"] is False
    assert body["candidates"] == []
    assert body["note"]


def test_needs_tools_flag_is_forwarded() -> None:
    client = TestClient(_make_app())
    resp = client.post(
        "/v1/econ/rightsizing", json={"incumbent": "gpt-4o", "needs_tools": True, "limit": 8}
    )
    assert resp.status_code == 200
    for c in resp.json()["candidates"]:
        assert c["supports_tools"] is True


def test_missing_incumbent_is_422() -> None:
    client = TestClient(_make_app())
    resp = client.post("/v1/econ/rightsizing", json={"limit": 5})
    assert resp.status_code == 422


# --- Measured experiment endpoint (network-free paths) -------------------------


def _experiment_body(**overrides) -> dict:
    body = {
        "node_id": "agent",
        "incumbent": "openai/gpt-4o",
        "instruction": "answer the question",
    }
    body.update(overrides)
    return body


def test_experiment_503_without_audit_repository() -> None:
    client = TestClient(_make_app())  # no bootstrap on app.state
    resp = client.post("/v1/econ/rightsizing/experiment", json=_experiment_body())
    assert resp.status_code == 503


def test_experiment_unknown_incumbent_returns_note() -> None:
    client = TestClient(_make_app(bootstrap=_bootstrap_with_audits([])))
    resp = client.post(
        "/v1/econ/rightsizing/experiment",
        json=_experiment_body(incumbent="totally-made-up-model-xyz"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["verdict"] == "none"
    assert body["outcomes"] == []
    assert "pricing" in body["note"].lower()


def test_experiment_no_history_asks_for_traffic() -> None:
    # Known incumbent → real candidates exist, but the node has no audit records, so the
    # experiment short-circuits before any LLM call.
    client = TestClient(_make_app(bootstrap=_bootstrap_with_audits([])))
    resp = client.post("/v1/econ/rightsizing/experiment", json=_experiment_body())
    assert resp.status_code == 200
    body = resp.json()
    assert body["verdict"] == "none"
    assert "real traffic" in body["note"]


def test_experiment_correctness_mode_without_labels_asks_for_verdicts() -> None:
    # Correctness mode with a known incumbent + candidates but no labeled runs → a 200 that
    # returns mode="correctness" and asks for expected-answer verdicts (no LLM call made).
    client = TestClient(_make_app(bootstrap=_bootstrap_with_audits([], runs=[])))
    resp = client.post("/v1/econ/rightsizing/experiment", json=_experiment_body(mode="correctness"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "correctness"
    assert body["verdict"] == "none"
    assert "labeled" in body["note"].lower() or "expected answer" in body["note"].lower()


def test_experiment_missing_fields_is_422() -> None:
    client = TestClient(_make_app(bootstrap=_bootstrap_with_audits([])))
    resp = client.post("/v1/econ/rightsizing/experiment", json={"node_id": "agent"})
    assert resp.status_code == 422


# --- Opportunities endpoint (Mode C) -------------------------------------------


def test_opportunities_503_without_audit_repository() -> None:
    client = TestClient(_make_app())
    resp = client.get("/v1/econ/rightsizing/opportunities")
    assert resp.status_code == 503


def test_opportunities_empty_when_no_spend() -> None:
    client = TestClient(_make_app(bootstrap=_bootstrap_with_audits([])))
    resp = client.get("/v1/econ/rightsizing/opportunities")
    assert resp.status_code == 200
    body = resp.json()
    assert body["nodes"] == []
    assert body["total_cost_usd"] == 0


def test_opportunities_ranks_spending_nodes() -> None:
    from zeroth.governance.audit.models import NodeAuditRecord, TokenUsage

    def _rec(node_id: str, cost: float) -> NodeAuditRecord:
        return NodeAuditRecord(
            audit_id=f"{node_id}-{cost}",
            run_id="r",
            node_id=node_id,
            graph_version_ref="g",
            deployment_ref="default",
            status="completed",
            cost_usd=cost,
            token_usage=TokenUsage(input_tokens=1000, output_tokens=200, model_name="gpt-4o"),
        )

    client = TestClient(
        _make_app(bootstrap=_bootstrap_with_audits([_rec("big", 0.9), _rec("small", 0.1)]))
    )
    resp = client.get("/v1/econ/rightsizing/opportunities")
    assert resp.status_code == 200
    body = resp.json()
    assert body["nodes"][0]["node_id"] == "big"
    assert body["nodes"][0]["experiment_ready"] is True
