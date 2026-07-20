"""Tests for the econ analytics REST API (unit economics, ECON-UNIT-01)."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from zeroth.governance.audit.models import NodeAuditRecord
from zeroth.governance.identity import AuthenticatedPrincipal, AuthMethod, ServiceRole
from zeroth.core.runs.models import Run, RunStatus
from zeroth.core.service.econ_analytics_api import register_econ_analytics_routes


def _make_app(*, bootstrap: object | None = None) -> FastAPI:
    """Minimal app with econ-analytics routes and an injected ADMIN principal."""
    app = FastAPI()
    if bootstrap is not None:
        app.state.bootstrap = bootstrap
    principal = AuthenticatedPrincipal(
        subject="test", auth_method=AuthMethod.API_KEY, roles=[ServiceRole.ADMIN]
    )

    @app.middleware("http")
    async def _inject_principal(request, call_next):
        request.state.principal = principal
        return await call_next(request)

    router = APIRouter(prefix="/v1")
    register_econ_analytics_routes(router)
    app.include_router(router)
    return app


def _bootstrap(runs: list | None = None, audits: list | None = None):
    """A fake bootstrap: run_repository (list/get/put) + audit_repository.list."""

    store = {r.run_id: r for r in (runs or [])}

    async def _list_runs(deployment_ref, *, status=None, limit=50, offset=0):
        return list(store.values())

    async def _list_audits(query):
        return list(audits or [])

    async def _get(run_id):
        return store.get(run_id)

    async def _put(run):
        store[run.run_id] = run
        return run

    return SimpleNamespace(
        deployment=SimpleNamespace(
            deployment_ref="default", tenant_id="default", workspace_id=None
        ),
        run_repository=SimpleNamespace(list_runs=_list_runs, get=_get, put=_put),
        audit_repository=SimpleNamespace(list=_list_audits),
    )


def _run(run_id: str, status: RunStatus) -> Run:
    return Run(run_id=run_id, graph_version_ref="g", deployment_ref="default", status=status)


def _audit(run_id: str, cost: float) -> NodeAuditRecord:
    return NodeAuditRecord(
        audit_id=f"{run_id}-{cost}",
        run_id=run_id,
        node_id="agent",
        graph_version_ref="g",
        deployment_ref="default",
        status="completed",
        cost_usd=cost,
    )


def test_503_without_repositories() -> None:
    client = TestClient(_make_app())  # no bootstrap on app.state
    resp = client.get("/v1/econ/unit-economics")
    assert resp.status_code == 503


def test_empty_reports_no_history() -> None:
    client = TestClient(_make_app(bootstrap=_bootstrap()))
    resp = client.get("/v1/econ/unit-economics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["window_runs"] == 0
    assert body["cost_per_successful_run_usd"] is None
    assert "no runs" in body["note"].lower()


def test_populated_headline_and_failure_tax() -> None:
    runs = [
        _run("s1", RunStatus.COMPLETED),
        _run("s2", RunStatus.COMPLETED),
        _run("f1", RunStatus.FAILED),
    ]
    audits = [_audit("s1", 0.10), _audit("s2", 0.10), _audit("f1", 0.20)]
    client = TestClient(_make_app(bootstrap=_bootstrap(runs, audits)))
    resp = client.get("/v1/econ/unit-economics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["successful_runs"] == 2
    assert body["failed_runs"] == 1
    assert body["cost_per_successful_run_usd"] == 0.20  # terminal 0.40 / 2 successes
    assert body["failure_tax_usd"] == 0.20
    assert body["by_workflow"][0]["workflow_name"] == "default"


def test_runs_without_cost_note_is_cause_neutral() -> None:
    runs = [_run("s1", RunStatus.COMPLETED)]
    client = TestClient(_make_app(bootstrap=_bootstrap(runs, [])))
    resp = client.get("/v1/econ/unit-economics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["runs_with_cost"] == 0
    assert "no priced model calls" in body["note"].lower()


def test_window_param_is_validated() -> None:
    client = TestClient(_make_app(bootstrap=_bootstrap()))
    assert client.get("/v1/econ/unit-economics?window=0").status_code == 422
    assert client.get("/v1/econ/unit-economics?window=5000").status_code == 422


def test_waste_503_without_repositories() -> None:
    client = TestClient(_make_app())
    assert client.get("/v1/econ/waste").status_code == 503


def test_waste_empty_reports_no_history() -> None:
    client = TestClient(_make_app(bootstrap=_bootstrap()))
    resp = client.get("/v1/econ/waste")
    assert resp.status_code == 200
    body = resp.json()
    assert body["window_runs"] == 0
    assert "no runs" in body["note"].lower()


def test_waste_rolls_up_confirmed_waste_over_runs() -> None:
    runs = [_run("f1", RunStatus.FAILED), _run("s1", RunStatus.COMPLETED)]
    audits = [_audit("f1", 0.20), _audit("s1", 0.10)]
    client = TestClient(_make_app(bootstrap=_bootstrap(runs, audits)))
    resp = client.get("/v1/econ/waste")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_confirmed_waste_usd"] == 0.20  # the failed run's whole spend
    assert body["runs_with_waste"] == 1
    assert body["top_findings"][0]["run_id"] == "f1"


def test_waste_limit_param_is_validated() -> None:
    client = TestClient(_make_app(bootstrap=_bootstrap()))
    assert client.get("/v1/econ/waste?limit=0").status_code == 422
    assert client.get("/v1/econ/waste?limit=99").status_code == 422


def test_unit_economics_includes_quality_overlay_not_configured() -> None:
    runs = [_run("s1", RunStatus.COMPLETED)]
    client = TestClient(_make_app(bootstrap=_bootstrap(runs, [_audit("s1", 0.10)])))
    body = client.get("/v1/econ/unit-economics").json()
    assert body["quality"]["state"] == "not_configured"
    assert body["quality"]["cost_per_quality_success_usd"] is None


def test_quality_verdict_persists_and_unit_economics_reflects_it() -> None:
    runs = [_run("s1", RunStatus.COMPLETED)]
    bootstrap = _bootstrap(runs, [_audit("s1", 0.10)])
    client = TestClient(_make_app(bootstrap=bootstrap))

    resp = client.post(
        "/v1/econ/quality-verdict",
        json={"run_id": "s1", "verdict": "good", "source": "human:alice"},
    )
    assert resp.status_code == 200
    assert resp.json()["verdict"] == "good"

    # The overlay now sees the labeled run.
    q = client.get("/v1/econ/unit-economics").json()["quality"]
    assert q["state"] == "ok"
    assert q["labeled_terminal_runs"] == 1
    assert q["quality_successes"] == 1
    assert q["cost_per_quality_success_usd"] == 0.10
    assert q["sources"] == ["human:alice"]


def test_quality_verdict_404_for_unknown_run() -> None:
    client = TestClient(_make_app(bootstrap=_bootstrap()))
    resp = client.post(
        "/v1/econ/quality-verdict", json={"run_id": "nope", "verdict": "good", "source": "x"}
    )
    assert resp.status_code == 404


def test_quality_verdict_409_for_non_terminal_run() -> None:
    runs = [_run("live", RunStatus.RUNNING)]
    client = TestClient(_make_app(bootstrap=_bootstrap(runs)))
    resp = client.post(
        "/v1/econ/quality-verdict", json={"run_id": "live", "verdict": "good", "source": "x"}
    )
    assert resp.status_code == 409
