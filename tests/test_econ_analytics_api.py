"""Tests for the econ analytics REST API (unit economics, ECON-UNIT-01)."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from zeroth.governance.audit.models import NodeAuditRecord
from zeroth.governance.identity import AuthenticatedPrincipal, AuthMethod, ServiceRole
from zeroth.runtime.runs import Run, RunStatus
from zeroth.service.api.econ_analytics_api import (
    _AUDIT_RECORDS_PER_RUN_BOUND,
    register_econ_analytics_routes,
)
from zeroth.service.api.rightsizing_api import _AUDIT_READ_BOUND, register_rightsizing_routes


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


def _make_rightsizing_app(*, bootstrap: object | None = None) -> FastAPI:
    """Build the same minimal app over the right-sizing routes.

    The other two bounded audit reads this requirement covers live in
    ``rightsizing_api``; they are driven from here so one check exercises all
    three call sites.
    """
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
    register_rightsizing_routes(router)
    app.include_router(router)
    return app


def _bootstrap(runs: list | None = None, audits: list | None = None):
    """Build a fake bootstrap: run_repository (list/get/put) + audit_repository.list.

    ``audit_limits`` records the ``limit`` each caller passed. A fake that merely
    *accepts* ``limit`` proves nothing -- widening the signature is exactly how
    this check stayed green with the bound deleted from all three call sites.
    """
    store = {r.run_id: r for r in (runs or [])}
    audit_limits: list[int | None] = []
    put_calls: list[str] = []
    metadata_merge_calls: list[tuple[str, dict]] = []

    async def _list_runs(deployment_ref, *, status=None, limit=50, offset=0):
        return [run for run in store.values() if run.deployment_ref == deployment_ref]

    async def _list_runs_for_scope(*, status=None, limit=50, offset=0):
        return list(store.values())[offset : offset + limit]

    async def _list_audits(query, *, limit=None):
        audit_limits.append(limit)
        records = list(audits or [])
        return records if limit is None else records[-limit:]

    async def _get(run_id):
        return store.get(run_id)

    async def _put(run):
        put_calls.append(run.run_id)
        store[run.run_id] = run
        return run

    async def _merge_terminal_metadata(run_id, patch):
        metadata_merge_calls.append((run_id, patch))
        run = store[run_id]
        run.metadata = {**(run.metadata or {}), **patch}
        return run

    return SimpleNamespace(
        deployment=SimpleNamespace(
            deployment_ref="default", tenant_id="default", workspace_id=None
        ),
        run_repository=SimpleNamespace(
            list_runs=_list_runs,
            list_runs_for_scope=_list_runs_for_scope,
            get=_get,
            put=_put,
            merge_terminal_metadata=_merge_terminal_metadata,
        ),
        audit_repository=SimpleNamespace(list=_list_audits),
        audit_limits=audit_limits,
        put_calls=put_calls,
        metadata_merge_calls=metadata_merge_calls,
    )


def _run(run_id: str, status: RunStatus) -> Run:
    return Run(run_id=run_id, graph_version_ref="g", deployment_ref="default", status=status)


def _audit(
    run_id: str, cost: float | None, *, estimated_cost: float | None = None
) -> NodeAuditRecord:
    return NodeAuditRecord(
        tenant_id="default",
        workspace_id=None,
        audit_id=f"{run_id}-{cost}",
        run_id=run_id,
        node_id="agent",
        graph_version_ref="g",
        deployment_ref="default",
        status="completed",
        cost_usd=cost,
        estimated_cost_usd=estimated_cost,
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


def test_economics_configuration_reports_runtime_ceiling_and_scope() -> None:
    bootstrap = _bootstrap()
    bootstrap.orchestrator = SimpleNamespace(per_run_cap_usd=0.25)
    bootstrap.budget_enforcer = SimpleNamespace(_fail_closed=True)
    bootstrap.regulus_client = SimpleNamespace(fail_closed=False)
    client = TestClient(_make_app(bootstrap=bootstrap))

    response = client.get("/v1/econ/configuration")

    assert response.status_code == 200
    assert response.json() == {
        "tenant_id": "default",
        "deployment_ref": "default",
        "per_run_cap_usd": 0.25,
        "failure_mode": "fail_closed",
        "source": "service_runtime",
    }


def test_economics_configuration_preserves_unconfigured_ceiling() -> None:
    bootstrap = _bootstrap()
    bootstrap.orchestrator = SimpleNamespace(per_run_cap_usd=None)
    bootstrap.budget_enforcer = SimpleNamespace(_fail_closed=False)
    client = TestClient(_make_app(bootstrap=bootstrap))

    response = client.get("/v1/econ/configuration")

    assert response.status_code == 200
    assert response.json()["per_run_cap_usd"] is None
    assert response.json()["failure_mode"] == "fail_open"


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


def test_estimated_failure_tax_provenance_survives_api_serialization() -> None:
    client = TestClient(
        _make_app(
            bootstrap=_bootstrap(
                [_run("f1", RunStatus.FAILED)],
                [_audit("f1", None, estimated_cost=0.20)],
            )
        )
    )

    body = client.get("/v1/econ/unit-economics").json()
    assert body["failure_tax_usd"] == 0.0
    assert body["estimated_failure_tax_usd"] == 0.20
    assert body["runs_with_cost"] == 0
    assert body["runs_with_estimated_cost"] == 1
    assert body["by_workflow"][0]["estimated_failure_tax_usd"] == 0.20


def test_tenant_scope_includes_runs_from_other_deployments() -> None:
    serving = _run("serving", RunStatus.COMPLETED)
    other = Run(
        run_id="other",
        graph_version_ref="other@1",
        deployment_ref="other-deployment",
        status=RunStatus.COMPLETED,
    )
    client = TestClient(
        _make_app(bootstrap=_bootstrap([serving, other], [_audit("serving", 0), _audit("other", 0)]))
    )

    response = client.get("/v1/econ/unit-economics?scope=tenant")

    assert response.status_code == 200
    assert response.json()["window_runs"] == 2


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


def test_quality_verdict_uses_nonrewriting_metadata_merge() -> None:
    bootstrap = _bootstrap([_run("s1", RunStatus.COMPLETED)])
    client = TestClient(_make_app(bootstrap=bootstrap))

    response = client.post(
        "/v1/econ/quality-verdict",
        json={"run_id": "s1", "verdict": "good", "source": "human:alice"},
    )

    assert response.status_code == 200
    assert bootstrap.put_calls == []
    assert bootstrap.metadata_merge_calls == [
        (
            "s1",
            {
                "quality_verdict": response.json(),
            },
        )
    ]


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


# --- ZER-48 / A02-14: every audit read passes an explicit bound ----------------
#
# The runs half of these endpoints was always capped; the audit half fetched the
# deployment's whole history and discarded most of it in Python. The bound is a
# keyword at the call site, and the only thing that can observe it is the
# repository -- so these assert on what the repository was *handed*, not on what
# the endpoint returned. A response-shape assertion is identical with and
# without ``limit=``, which is how the original check passed on reverted code.


def test_unit_economics_audit_read_carries_an_explicit_bound() -> None:
    """The unit-economics audit read is bounded, and bounded by the window."""
    bootstrap = _bootstrap()
    client = TestClient(_make_app(bootstrap=bootstrap))

    assert client.get("/v1/econ/unit-economics?window=7").status_code == 200

    assert len(bootstrap.audit_limits) == 1, "the audit repository was never read"
    limit = bootstrap.audit_limits[0]
    assert limit is not None, "the audit read fetched the whole deployment history"
    assert limit == 7 * _AUDIT_RECORDS_PER_RUN_BOUND


def test_the_unit_economics_audit_bound_scales_with_the_window() -> None:
    """The two halves of the read grow together instead of one growing freely."""
    bootstrap = _bootstrap()
    client = TestClient(_make_app(bootstrap=bootstrap))

    client.get("/v1/econ/unit-economics?window=10")
    client.get("/v1/econ/unit-economics?window=20")

    assert None not in bootstrap.audit_limits
    assert bootstrap.audit_limits[1] == 2 * bootstrap.audit_limits[0]


def test_waste_audit_read_carries_an_explicit_bound() -> None:
    """The waste rollup reads through the same bounded helper."""
    bootstrap = _bootstrap()
    client = TestClient(_make_app(bootstrap=bootstrap))

    assert client.get("/v1/econ/waste?window=5").status_code == 200

    assert len(bootstrap.audit_limits) == 1, "the audit repository was never read"
    assert bootstrap.audit_limits[0] == 5 * _AUDIT_RECORDS_PER_RUN_BOUND


def test_rightsizing_opportunities_audit_read_carries_an_explicit_bound() -> None:
    """``rightsizing_api.py:117`` -- the spend rollup passes a bound."""
    bootstrap = _bootstrap()
    client = TestClient(_make_rightsizing_app(bootstrap=bootstrap))

    assert client.get("/v1/econ/rightsizing/opportunities").status_code == 200

    assert len(bootstrap.audit_limits) == 1, "the audit repository was never read"
    assert bootstrap.audit_limits[0] is not None
    assert bootstrap.audit_limits[0] == _AUDIT_READ_BOUND


def test_rightsizing_experiment_audit_read_carries_an_explicit_bound() -> None:
    """``rightsizing_api.py:163`` -- the replay harvest passes a bound.

    A known incumbent with no audit history short-circuits before any LLM call,
    which is after the audit read this asserts on.
    """
    bootstrap = _bootstrap()
    client = TestClient(_make_rightsizing_app(bootstrap=bootstrap))

    resp = client.post(
        "/v1/econ/rightsizing/experiment",
        json={
            "node_id": "agent",
            "incumbent": "openai/gpt-4o",
            "instruction": "answer the question",
        },
    )
    assert resp.status_code == 200

    assert len(bootstrap.audit_limits) == 1, (
        "the audit repository was never read -- the endpoint short-circuited before "
        "reaching the bounded call site, so this test proves nothing about it"
    )
    assert bootstrap.audit_limits[0] is not None
    assert bootstrap.audit_limits[0] == _AUDIT_READ_BOUND
