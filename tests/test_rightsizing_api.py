"""Tests for the model right-sizing REST API endpoints."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from zeroth.governance.identity import AuthenticatedPrincipal, AuthMethod, ServiceRole
from zeroth.governance.audit.models import NodeAuditRecord, TokenUsage
from zeroth.governance.audit.capture_policy import AuditCapturePolicy
from zeroth.runtime.agents.provider import ProviderResponse
from zeroth.runtime.runs import Run, RunHistoryEntry, RunStatus
from zeroth.econ.analytics.cost import CostEstimator
from zeroth.service.api.rightsizing_api import register_rightsizing_routes


def _make_app(
    *,
    roles: list[ServiceRole] | None = None,
    bootstrap: object | None = None,
    tenant_id: str = "default",
    workspace_id: str | None = None,
) -> FastAPI:
    """Minimal app with right-sizing routes and an injected principal (defaults ADMIN)."""
    app = FastAPI()
    if bootstrap is not None:
        app.state.bootstrap = bootstrap
    principal = AuthenticatedPrincipal(
        subject="test",
        auth_method=AuthMethod.API_KEY,
        roles=roles if roles is not None else [ServiceRole.ADMIN],
        tenant_id=tenant_id,
        workspace_id=workspace_id,
    )

    @app.middleware("http")
    async def _inject_principal(request, call_next):
        request.state.principal = principal
        return await call_next(request)

    router = APIRouter(prefix="/v1")
    register_rightsizing_routes(router)
    app.include_router(router)
    return app


def _bootstrap_with_audits(
    records: list | None = None,
    runs: list | None = None,
    child_runs: dict[str, list] | None = None,
    *,
    tenant_id: str = "default",
    workspace_id: str | None = None,
):
    """A fake bootstrap whose audit_repository.list returns ``records`` (+ optional runs)."""

    written = []
    created = []

    async def _list(query, *, limit=None):
        rows = list(records or [])
        deployment_ref = getattr(query, "deployment_ref", None)
        run_id = getattr(query, "run_id", None)
        if deployment_ref is not None:
            rows = [row for row in rows if row.deployment_ref == deployment_ref]
        if run_id is not None:
            rows = [row for row in rows if row.run_id == run_id]
        return rows if limit is None else rows[-limit:]

    async def _write(record):
        captured = AuditCapturePolicy().apply(record)
        signed = captured.model_copy(
            update={
                "record_signature": f"signature:{record.audit_id}",
                "signing_key_id": "test-key",
                "signing_algorithm": "HMAC-SHA256",
            }
        )
        written.append(signed)
        return signed

    async def _create(run):
        created.append(run)
        return run

    async def _put(run):
        created[-1] = run
        return run

    async def _list_runs(deployment_ref, *, status=None, limit=50, offset=0):
        return list(runs or [])

    async def _list_child_runs(parent_run_id):
        return list((child_runs or {}).get(parent_run_id, []))

    return SimpleNamespace(
        deployment=SimpleNamespace(
            deployment_ref="default", tenant_id=tenant_id, workspace_id=workspace_id
        ),
        audit_repository=SimpleNamespace(list=_list, write=_write, _signer=object(), rows=written),
        run_repository=SimpleNamespace(
            list_runs=_list_runs,
            list_child_runs=_list_child_runs,
            create=_create,
            put=_put,
            rows=created,
        ),
    )


def _stored_rightsizing_run(
    run_id: str,
    *,
    tenant_id: str = "default",
    workspace_id: str | None = None,
    node_id: str = "agent",
    status: RunStatus = RunStatus.COMPLETED,
) -> Run:
    return Run(
        run_id=run_id,
        graph_version_ref="graph@1",
        deployment_ref="default",
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        status=status,
        metadata={"execution_kind": "rightsizing_experiment"},
        final_output={
            "rightsizing_experiment_report": {
                "incumbent": "gpt-4o",
                "node_id": node_id,
                "cases": 1,
                "min_cases": 5,
                "verdict": "flagged",
                "note": "Restored measured result.",
            }
        },
    )


def test_latest_experiment_returns_null_when_no_completed_result_exists() -> None:
    bootstrap = _bootstrap_with_audits(runs=[])

    response = TestClient(_make_app(bootstrap=bootstrap)).get(
        "/v1/econ/rightsizing/experiment/latest"
    )

    assert response.status_code == 200
    assert response.json() is None


def test_latest_experiment_ignores_an_incomplete_stored_result() -> None:
    incomplete = _stored_rightsizing_run(
        "rightsizing:incomplete",
        status=RunStatus.FAILED,
    )
    bootstrap = _bootstrap_with_audits(runs=[incomplete])

    response = TestClient(_make_app(bootstrap=bootstrap)).get(
        "/v1/econ/rightsizing/experiment/latest"
    )

    assert response.status_code == 200
    assert response.json() is None


def test_latest_experiment_reads_back_the_newest_completed_result() -> None:
    newest = _stored_rightsizing_run("rightsizing:newest", node_id="newest-node")
    older = _stored_rightsizing_run("rightsizing:older", node_id="older-node")
    bootstrap = _bootstrap_with_audits(runs=[newest, older])

    response = TestClient(_make_app(bootstrap=bootstrap)).get(
        "/v1/econ/rightsizing/experiment/latest"
    )

    assert response.status_code == 200
    assert response.json()["node_id"] == "newest-node"
    assert response.json()["note"] == "Restored measured result."


def test_latest_experiment_does_not_read_other_tenant_or_workspace_results() -> None:
    wrong_tenant = _stored_rightsizing_run(
        "rightsizing:wrong-tenant", tenant_id="other", workspace_id="ws-a"
    )
    wrong_workspace = _stored_rightsizing_run(
        "rightsizing:wrong-workspace", tenant_id="acme", workspace_id="ws-b"
    )
    bootstrap = _bootstrap_with_audits(
        runs=[wrong_tenant, wrong_workspace], tenant_id="acme", workspace_id="ws-a"
    )

    response = TestClient(
        _make_app(
            bootstrap=bootstrap,
            tenant_id="acme",
            workspace_id="ws-a",
        )
    ).get("/v1/econ/rightsizing/experiment/latest")

    assert response.status_code == 200
    assert response.json() is None


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


def test_experiment_requires_metrics_admin_permission() -> None:
    client = TestClient(
        _make_app(roles=[ServiceRole.REVIEWER], bootstrap=_bootstrap_with_audits([]))
    )
    resp = client.post("/v1/econ/rightsizing/experiment", json=_experiment_body())
    assert resp.status_code == 403


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


def test_experiment_refuses_ambiguous_authored_node_across_deployments() -> None:
    records = [
        NodeAuditRecord(
            tenant_id="default",
            workspace_id=None,
            audit_id=f"audit-{deployment_ref}",
            run_id=f"run-{deployment_ref}",
            node_id=f"branch:0:subgraph:{deployment_ref}:1:agent",
            graph_version_ref="graph@1",
            deployment_ref=deployment_ref,
            status="completed",
            input_snapshot={"question": "What changed?"},
            output_snapshot={"content": "same answer"},
            token_usage=TokenUsage(
                input_tokens=20,
                output_tokens=10,
                model_name="openai/gpt-4o",
            ),
        )
        for deployment_ref in ("child-a", "child-b")
    ]
    parent = SimpleNamespace(run_id="parent", status="COMPLETED")
    children = [
        SimpleNamespace(
            run_id=f"run-{deployment_ref}",
            status="COMPLETED",
            parent_run_id="parent",
        )
        for deployment_ref in ("child-a", "child-b")
    ]
    client = TestClient(
        _make_app(
            bootstrap=_bootstrap_with_audits(
                records,
                runs=[parent],
                child_runs={"parent": children},
            )
        )
    )

    response = client.post(
        "/v1/econ/rightsizing/experiment",
        json=_experiment_body(node_id="agent"),
    )

    assert response.status_code == 200
    assert response.json()["verdict"] == "none"
    assert "multiple composed deployments" in response.json()["note"].lower()


def test_experiment_uses_scoped_secret_provider_and_persistent_run_cap(monkeypatch) -> None:
    """Every replay and judge call must share the campaign's persistent run ceiling."""

    class _Provider:
        async def ainvoke(self, request):
            content = (
                '{"score": 1.0, "rationale": "equivalent"}'
                if request.output_model is not None
                else "same answer"
            )
            return ProviderResponse(
                content=content,
                token_usage=TokenUsage(
                    input_tokens=20,
                    output_tokens=10,
                    model_name=request.model_name,
                ),
                metadata={"provider_request_id": "provider-request"},
            )

    class _Instrumentation:
        def __init__(self):
            self.reservations = []

        async def reserve_probe(self, **kwargs):
            self.reservations.append(kwargs)

        async def commit_probe(self, **kwargs):
            return SimpleNamespace(
                cost_event_id=f"event-{len(self.reservations)}",
                cost_measurement=kwargs["cost_measurement"],
                provider_request_id=kwargs.get("provider_request_id"),
                cleanup_status=kwargs["cleanup_status"],
            )

        async def release_probe(self, **kwargs):  # pragma: no cover - success path only
            raise AssertionError(f"unexpected release: {kwargs}")

        async def mark_probe_ambiguous(self, **kwargs):  # pragma: no cover
            raise AssertionError(f"unexpected ambiguous call: {kwargs}")

    constructor_kwargs = []

    def _provider_factory(**kwargs):
        constructor_kwargs.append(kwargs)
        return _Provider()

    monkeypatch.setattr(
        "zeroth.service.api.rightsizing_api.LiteLLMProviderAdapter", _provider_factory
    )
    instrumentation = _Instrumentation()
    secret_provider = object()
    record = NodeAuditRecord(
        tenant_id="default",
        workspace_id=None,
        audit_id="audit-1",
        run_id="source-run",
        node_id="branch:0:subgraph:child:1:agent",
        graph_version_ref="graph@1",
        deployment_ref="default",
        status="completed",
        # Signed audit capture deliberately omits payloads; measured replay must
        # join the same node's durable run-history snapshots without mutating
        # the audit record.
        input_snapshot={},
        output_snapshot={},
        token_usage=TokenUsage(
            input_tokens=20,
            output_tokens=10,
            model_name="openai/gpt-4o",
        ),
    )
    source_run = SimpleNamespace(
        run_id="source-run",
        status="COMPLETED",
        execution_history=[
            RunHistoryEntry(
                node_id="branch:0:subgraph:child:1:agent",
                status="completed",
                input_snapshot={"question": "What changed?"},
                output_snapshot={"content": "same answer"},
            )
        ],
    )
    bootstrap = _bootstrap_with_audits([record], runs=[source_run])
    bootstrap.secret_provider = secret_provider
    bootstrap.probe_instrumentation = instrumentation
    bootstrap.regulus_client = None
    bootstrap.cost_estimator = CostEstimator()
    bootstrap.orchestrator = SimpleNamespace(per_run_cap_usd=0.25)
    bootstrap.evaluation_campaign_id = "campaign-1"
    bootstrap.signer = object()

    client = TestClient(_make_app(bootstrap=bootstrap))
    resp = client.post(
        "/v1/econ/rightsizing/experiment",
        json=_experiment_body(max_candidates=1, max_cases=1, min_cases=1),
    )

    assert resp.status_code == 200
    assert constructor_kwargs == [
        {
            "secret_provider": secret_provider,
            "tenant_id": "default",
            "allow_env_fallback": False,
        }
    ]
    # One incumbent replay + judge and one candidate replay + judge.
    assert len(instrumentation.reservations) == 4
    assert {row["campaign_id"] for row in instrumentation.reservations} == {"campaign-1"}
    assert {row["run_cap_usd"] for row in instrumentation.reservations} == {"0.25"}
    assert len({row["operation_id"] for row in instrumentation.reservations}) == 4
    assert len({row["run_id"] for row in instrumentation.reservations}) == 1
    execution = resp.json()["execution"]
    assert execution["run_id"].startswith("rightsizing:")
    assert execution["campaign_id"] == "campaign-1"
    assert execution["provider_call_count"] == 4
    assert len(execution["calls"]) == 4
    assert {row["operation_id"] for row in execution["calls"]} == {
        row["operation_id"] for row in instrumentation.reservations
    }
    assert {row["cost_event_id"] for row in execution["calls"]} == {
        "event-1",
        "event-2",
        "event-3",
        "event-4",
    }
    assert all(row["cleanup_status"] == "complete" for row in execution["calls"])
    assert execution["estimated_cost_usd"] > 0
    assert execution["measured_cost_usd"] == 0
    assert len(bootstrap.run_repository.rows) == 1
    persisted_run = bootstrap.run_repository.rows[0]
    assert persisted_run.run_id == execution["run_id"]
    assert persisted_run.status.value == "COMPLETED"
    assert persisted_run.metadata == {
        "campaign_id": "campaign-1",
        "campaign_strict": True,
        "dispatchable": False,
        "execution_kind": "rightsizing_experiment",
    }
    assert persisted_run.final_output["rightsizing_experiment_report"] == resp.json()
    assert len(bootstrap.audit_repository.rows) == 4
    assert {row.run_id for row in bootstrap.audit_repository.rows} == {execution["run_id"]}
    assert {row.cost_event_id for row in bootstrap.audit_repository.rows} == {
        "event-1",
        "event-2",
        "event-3",
        "event-4",
    }
    assert all(row.record_signature for row in bootstrap.audit_repository.rows)
    assert {row.campaign_id for row in bootstrap.audit_repository.rows} == {"campaign-1"}
    assert all(
        "provider_request_id" not in row.execution_metadata
        for row in bootstrap.audit_repository.rows
    )
    assert {row.audit_id for row in bootstrap.audit_repository.rows} == {
        row["audit_event_id"] for row in execution["calls"]
    }


def test_experiment_fails_closed_before_provider_without_cost_instrumentation(
    monkeypatch,
) -> None:
    calls = []

    class _Provider:
        async def ainvoke(self, request):  # pragma: no cover - must remain unreachable
            calls.append(request)
            raise AssertionError("provider must not be called")

    monkeypatch.setattr(
        "zeroth.service.api.rightsizing_api.LiteLLMProviderAdapter",
        lambda **kwargs: _Provider(),
    )
    record = NodeAuditRecord(
        tenant_id="default",
        workspace_id=None,
        audit_id="audit-1",
        run_id="source-run",
        node_id="agent",
        graph_version_ref="graph@1",
        deployment_ref="default",
        status="completed",
        input_snapshot={"question": "What changed?"},
        output_snapshot={"content": "same answer"},
        token_usage=TokenUsage(
            input_tokens=20,
            output_tokens=10,
            model_name="openai/gpt-4o",
        ),
    )
    bootstrap = _bootstrap_with_audits([record])
    bootstrap.secret_provider = object()
    bootstrap.probe_instrumentation = None
    bootstrap.cost_estimator = CostEstimator()
    bootstrap.orchestrator = SimpleNamespace(per_run_cap_usd=0.25)

    client = TestClient(_make_app(bootstrap=bootstrap))
    resp = client.post(
        "/v1/econ/rightsizing/experiment",
        json=_experiment_body(max_candidates=1, max_cases=1),
    )

    assert resp.status_code == 503
    assert "persistent cost instrumentation" in resp.json()["detail"]
    assert calls == []


def test_experiment_fails_closed_before_provider_without_audit_signing(monkeypatch) -> None:
    calls = []

    class _Provider:
        async def ainvoke(self, request):  # pragma: no cover - must remain unreachable
            calls.append(request)
            raise AssertionError("provider must not be called")

    monkeypatch.setattr(
        "zeroth.service.api.rightsizing_api.LiteLLMProviderAdapter",
        lambda **kwargs: _Provider(),
    )
    record = NodeAuditRecord(
        tenant_id="default",
        workspace_id=None,
        audit_id="audit-1",
        run_id="source-run",
        node_id="agent",
        graph_version_ref="graph@1",
        deployment_ref="default",
        status="completed",
        input_snapshot={"question": "What changed?"},
        output_snapshot={"content": "same answer"},
        token_usage=TokenUsage(
            input_tokens=20,
            output_tokens=10,
            model_name="openai/gpt-4o",
        ),
    )
    bootstrap = _bootstrap_with_audits([record])
    bootstrap.secret_provider = object()
    bootstrap.probe_instrumentation = object()
    bootstrap.cost_estimator = CostEstimator()
    bootstrap.orchestrator = SimpleNamespace(per_run_cap_usd=0.25)
    bootstrap.evaluation_campaign_id = "campaign-1"
    bootstrap.signer = None

    client = TestClient(_make_app(bootstrap=bootstrap))
    response = client.post(
        "/v1/econ/rightsizing/experiment",
        json=_experiment_body(max_candidates=1, max_cases=1),
    )

    assert response.status_code == 503
    assert "audit signing" in response.json()["detail"]
    assert bootstrap.run_repository.rows == []
    assert calls == []


def test_experiment_failure_updates_the_durable_non_dispatchable_run(monkeypatch) -> None:
    async def _fail_experiment(**kwargs):
        del kwargs
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr("zeroth.service.api.rightsizing_api.run_experiment", _fail_experiment)
    monkeypatch.setattr(
        "zeroth.service.api.rightsizing_api.LiteLLMProviderAdapter",
        lambda **kwargs: object(),
    )
    record = NodeAuditRecord(
        tenant_id="default",
        workspace_id=None,
        audit_id="audit-1",
        run_id="source-run",
        node_id="agent",
        graph_version_ref="graph@1",
        deployment_ref="default",
        status="completed",
        input_snapshot={"question": "What changed?"},
        output_snapshot={"content": "same answer"},
        token_usage=TokenUsage(
            input_tokens=20,
            output_tokens=10,
            model_name="openai/gpt-4o",
        ),
    )
    bootstrap = _bootstrap_with_audits([record])
    bootstrap.secret_provider = object()
    bootstrap.probe_instrumentation = object()
    bootstrap.cost_estimator = CostEstimator()
    bootstrap.orchestrator = SimpleNamespace(per_run_cap_usd=0.25)
    bootstrap.evaluation_campaign_id = "campaign-1"
    bootstrap.signer = object()

    client = TestClient(_make_app(bootstrap=bootstrap), raise_server_exceptions=False)
    response = client.post(
        "/v1/econ/rightsizing/experiment",
        json=_experiment_body(max_candidates=1, max_cases=1),
    )

    assert response.status_code == 500
    assert len(bootstrap.run_repository.rows) == 1
    failed = bootstrap.run_repository.rows[0]
    assert failed.status.value == "FAILED"
    assert failed.failure_state.reason == "rightsizing_experiment_failed"
    assert failed.metadata["dispatchable"] is False
    assert failed.final_output == {"provider_call_count": 0, "verdict": "failed"}


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


def test_opportunities_503_without_run_repository() -> None:
    bootstrap = SimpleNamespace(
        deployment=SimpleNamespace(
            deployment_ref="default", tenant_id="default", workspace_id=None
        ),
        audit_repository=SimpleNamespace(list=lambda *_args, **_kwargs: []),
    )
    client = TestClient(_make_app(bootstrap=bootstrap))

    response = client.get("/v1/econ/rightsizing/opportunities")

    assert response.status_code == 503
    assert response.json()["detail"] == "run repository not configured"


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
            tenant_id="default",
            workspace_id=None,
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
        _make_app(
            bootstrap=_bootstrap_with_audits(
                [_rec("big", 0.9), _rec("small", 0.1)],
                runs=[SimpleNamespace(run_id="r", status="COMPLETED")],
            )
        )
    )
    resp = client.get("/v1/econ/rightsizing/opportunities")
    assert resp.status_code == 200
    body = resp.json()
    assert body["nodes"][0]["node_id"] == "big"
    assert body["nodes"][0]["experiment_ready"] is True


def test_opportunities_include_replayable_composed_child_runs() -> None:
    parent = SimpleNamespace(run_id="parent", status="COMPLETED")
    child = SimpleNamespace(run_id="child", status="COMPLETED", parent_run_id="parent")
    child_record = NodeAuditRecord(
        tenant_id="default",
        workspace_id=None,
        audit_id="child-agent-audit",
        run_id="child",
        node_id="research",
        graph_version_ref="child-graph@1",
        deployment_ref="child-deployment",
        status="completed",
        estimated_cost_usd=0.25,
        cost_event_id="child-cost-event",
        token_usage=TokenUsage(
            input_tokens=1000,
            output_tokens=200,
            model_name="openai/gpt-4o",
        ),
    )
    client = TestClient(
        _make_app(
            bootstrap=_bootstrap_with_audits(
                [child_record],
                runs=[parent],
                child_runs={"parent": [child]},
            )
        )
    )

    response = client.get("/v1/econ/rightsizing/opportunities")

    assert response.status_code == 200
    body = response.json()
    assert body["total_estimated_cost_usd"] == 0.25
    assert body["nodes"][0]["node_id"] == "research"
    assert body["nodes"][0]["runs"] == 1
    assert body["nodes"][0]["experiment_ready"] is True


def test_opportunities_serializes_estimated_spend_separately() -> None:
    record = NodeAuditRecord(
        tenant_id="default",
        workspace_id=None,
        audit_id="estimated-agent",
        run_id="r",
        node_id="agent",
        graph_version_ref="g",
        deployment_ref="default",
        status="completed",
        estimated_cost_usd=0.25,
        token_usage=TokenUsage(input_tokens=1000, output_tokens=200, model_name="gpt-4o"),
    )
    client = TestClient(
        _make_app(
            bootstrap=_bootstrap_with_audits(
                [record], runs=[SimpleNamespace(run_id="r", status="COMPLETED")]
            )
        )
    )

    body = client.get("/v1/econ/rightsizing/opportunities").json()
    assert body["total_cost_usd"] == 0.0
    assert body["total_estimated_cost_usd"] == 0.25
    assert body["nodes"][0]["total_cost_usd"] == 0.0
    assert body["nodes"][0]["total_estimated_cost_usd"] == 0.25


def test_opportunities_excludes_control_probe_without_persisted_workflow_run() -> None:
    from zeroth.governance.audit.models import NodeAuditRecord, TokenUsage

    def _rec(run_id: str, node_id: str, cost: float) -> NodeAuditRecord:
        return NodeAuditRecord(
            tenant_id="default",
            workspace_id=None,
            audit_id=f"{run_id}-{node_id}",
            run_id=run_id,
            node_id=node_id,
            graph_version_ref="g",
            deployment_ref="default",
            status="completed",
            cost_usd=cost,
            token_usage=TokenUsage(
                input_tokens=1000,
                output_tokens=200,
                model_name="openai/gpt-4o",
            ),
        )

    client = TestClient(
        _make_app(
            bootstrap=_bootstrap_with_audits(
                [
                    _rec("workflow-run", "agent", 0.01),
                    _rec("control-run:corpus-seed", "control.corpus-seed.embedding", 0.40),
                ],
                runs=[SimpleNamespace(run_id="workflow-run", status="COMPLETED")],
            )
        )
    )

    response = client.get("/v1/econ/rightsizing/opportunities")

    assert response.status_code == 200
    body = response.json()
    assert body["total_cost_usd"] == 0.01
    assert [node["node_id"] for node in body["nodes"]] == ["agent"]


def test_opportunities_explains_when_only_control_plane_spend_exists() -> None:
    from zeroth.governance.audit.models import NodeAuditRecord, TokenUsage

    record = NodeAuditRecord(
        tenant_id="default",
        workspace_id=None,
        audit_id="probe-audit",
        run_id="control-run:probe",
        node_id="control.provider-verification",
        graph_version_ref="g",
        deployment_ref="default",
        status="completed",
        cost_usd=0.00000128,
        token_usage=TokenUsage(
            input_tokens=8,
            output_tokens=0,
            model_name="openai/text-embedding-3-small",
        ),
    )
    client = TestClient(_make_app(bootstrap=_bootstrap_with_audits([record], runs=[])))

    response = client.get("/v1/econ/rightsizing/opportunities")

    assert response.status_code == 200
    body = response.json()
    assert body["nodes"] == []
    assert body["total_cost_usd"] == 0
    assert "control-plane probes are excluded" in body["note"].lower()
