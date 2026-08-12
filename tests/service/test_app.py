from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.routing import Match

from tests.graph.test_models import build_graph
from tests.service.helpers import default_service_auth_config, operator_headers, reviewer_headers
from zeroth.contracts.graph import GraphRepository
from zeroth.contracts.langgraph_gateway.models import CompatibilityResult, CompatibilityStatus
from zeroth.contracts.registry import ContractRegistry
from zeroth.governance.identity import AuthenticatedPrincipal, AuthMethod, ServiceRole
from zeroth.integrations.execution import ExecutableUnitRunner
from zeroth.platform.artifacts.tenant_scoped import TenantScopedArtifactStore
from zeroth.service.api.authentication import ServiceAuthConfig, ServiceAuthenticator
from zeroth.service.app import create_app
from zeroth.service.bootstrap.container import DeploymentBootstrapError
from zeroth.service.bootstrap.factory import bootstrap_app, bootstrap_service
from zeroth.service.deployments import DeploymentService, SQLiteDeploymentRepository


class AppInputContract(BaseModel):
    value: int


class AppOutputContract(BaseModel):
    value: int


async def _deploy_test_graph(sqlite_db, deployment_ref: str = "graph-1-service"):
    graph_repository = GraphRepository(sqlite_db)
    contract_registry = ContractRegistry.for_default_compatibility(sqlite_db)
    await contract_registry.register(AppInputContract, name="contract://input")
    await contract_registry.register(AppOutputContract, name="contract://output")
    deployment_service = DeploymentService(
        graph_repository=graph_repository,
        deployment_repository=SQLiteDeploymentRepository(sqlite_db),
        contract_registry=contract_registry,
    )
    graph = await graph_repository.create(build_graph())
    await graph_repository.publish(graph.graph_id, graph.version)
    return await deployment_service.deploy(deployment_ref, graph.graph_id, graph.version)


async def test_bootstrap_service_loads_valid_deployment(sqlite_db) -> None:
    deployment = await _deploy_test_graph(sqlite_db)

    service = await bootstrap_service(sqlite_db, deployment_ref=deployment.deployment_ref)

    assert service.deployment == deployment
    assert service.graph.graph_id == deployment.graph_id
    assert service.graph.version == deployment.graph_version
    assert service.run_repository is service.orchestrator.run_repository
    assert service.audit_repository is service.orchestrator.audit_repository
    assert service.approval_service is service.orchestrator.approval_service
    assert service.contract_registry is not None


async def test_bootstrap_wires_one_deployment_scoped_artifact_store(sqlite_db) -> None:
    deployment = await _deploy_test_graph(sqlite_db, "artifact-scope-service")

    service = await bootstrap_service(sqlite_db, deployment_ref=deployment.deployment_ref)

    assert isinstance(service.artifact_store, TenantScopedArtifactStore)
    assert service.orchestrator.artifact_store is service.artifact_store
    assert service.retention_erasure_service._artifact_store is service.artifact_store
    assert (
        service.artifact_store.scope_digest
        == TenantScopedArtifactStore(
            object(),
            tenant_id=deployment.tenant_id,
            workspace_id=deployment.workspace_id,
        ).scope_digest
    )


async def test_bootstrap_service_accepts_injected_runners(sqlite_db) -> None:
    deployment = await _deploy_test_graph(sqlite_db)
    agent_runner = object()
    executable_unit_runner = ExecutableUnitRunner()

    service = await bootstrap_service(
        sqlite_db,
        deployment_ref=deployment.deployment_ref,
        agent_runners={"agent-step": agent_runner},
        executable_unit_runner=executable_unit_runner,
    )

    assert service.orchestrator.agent_runners["agent-step"] is agent_runner
    assert service.orchestrator.executable_unit_runner is executable_unit_runner


async def test_bootstrap_app_forwards_injected_runners(sqlite_db) -> None:
    deployment = await _deploy_test_graph(sqlite_db)
    agent_runner = object()
    executable_unit_runner = ExecutableUnitRunner()

    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=deployment.deployment_ref,
        agent_runners={"agent-step": agent_runner},
        executable_unit_runner=executable_unit_runner,
        auth_config=default_service_auth_config(),
    )

    assert app.state.bootstrap.orchestrator.agent_runners["agent-step"] is agent_runner
    assert app.state.bootstrap.orchestrator.executable_unit_runner is executable_unit_runner


async def test_bootstrap_service_fails_for_missing_deployment(sqlite_db) -> None:
    with pytest.raises(DeploymentBootstrapError, match="missing-service"):
        await bootstrap_service(sqlite_db, deployment_ref="missing-service")


async def test_bootstrap_service_rejects_mismatched_graph_snapshot(sqlite_db, monkeypatch) -> None:
    deployment = await _deploy_test_graph(sqlite_db)

    original_graph = deployment.graph_id
    broken_graph = build_graph().model_copy(update={"graph_id": "graph-2", "version": 2})

    def fake_hydrate_deployed_graph(_deployment):
        return broken_graph

    monkeypatch.setattr(
        "zeroth.service.bootstrap.factory.hydrate_deployed_graph",
        fake_hydrate_deployed_graph,
    )

    with pytest.raises(DeploymentBootstrapError, match=original_graph):
        await bootstrap_service(sqlite_db, deployment_ref=deployment.deployment_ref)


async def test_health_endpoint_returns_success(sqlite_db) -> None:
    deployment = await _deploy_test_graph(sqlite_db)
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=deployment.deployment_ref,
        auth_config=default_service_auth_config(),
    )

    assert app.state.bootstrap.deployment == deployment

    with TestClient(app) as client:
        response = client.get("/health", headers=operator_headers())

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "deployment_ref": deployment.deployment_ref,
        "deployment_version": deployment.version,
        "graph_version_ref": deployment.graph_version_ref,
    }


async def test_lifespan_closes_secret_provider_exactly_once(sqlite_db) -> None:
    deployment = await _deploy_test_graph(sqlite_db, "graph-secret-close")
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=deployment.deployment_ref,
        auth_config=default_service_auth_config(),
    )

    closes = 0

    class _ClosableProvider:
        async def aclose(self) -> None:
            nonlocal closes
            closes += 1

    app.state.bootstrap.secret_provider = _ClosableProvider()
    with TestClient(app):
        pass
    assert closes == 1


async def test_gateway_disabled_constructs_no_gateway_dependencies(sqlite_db) -> None:
    deployment = await _deploy_test_graph(sqlite_db, "graph-gateway-disabled")

    service = await bootstrap_service(sqlite_db, deployment_ref=deployment.deployment_ref)

    assert service.langgraph_gateway_proxy is None
    assert service.langgraph_gateway_transport is None
    assert service.langgraph_gateway_compatibility is None
    assert service.langgraph_gateway_capability_reporter is None
    assert service.langgraph_gateway_websocket_handler is None


async def test_gateway_enabled_reuses_shared_dependencies(sqlite_db, monkeypatch) -> None:
    from zeroth.platform.config import LangGraphGatewaySettings, get_settings
    from zeroth.platform.signing import EnvHmacSigner

    deployment = await _deploy_test_graph(sqlite_db, "graph-gateway-enabled")
    gateway_settings = LangGraphGatewaySettings(
        enabled=True,
        upstream_url="http://agent-server.test",
        upstream_audience="agent-server:test",
        deployment_ref=deployment.deployment_ref,
    )
    settings = get_settings().model_copy(update={"langgraph_gateway": gateway_settings})
    signer = EnvHmacSigner(key_id="test", keys={"test": b"gateway-signing-key"})
    secret_provider = object()
    constructions: dict[str, object] = {}

    class FakeTransport:
        def __init__(self, configured_settings, configured_secret_provider) -> None:
            constructions["transport_settings"] = configured_settings
            constructions["transport_secret_provider"] = configured_secret_provider
            self.client = SimpleNamespace(base_url=None)

        async def aclose(self) -> None:
            pass

    class FakeDetector:
        def __init__(self, client, **kwargs) -> None:
            constructions["detector_client"] = client
            constructions["detector_kwargs"] = kwargs

        async def detect(self) -> CompatibilityResult:
            return CompatibilityResult(
                tested_langgraph_versions=("1.2.9",),
                tested_agent_server_versions=("0.11.1",),
                detected_agent_server_version="0.11.1",
                openapi_fingerprint="sha256:test",
                status=CompatibilityStatus.SUPPORTED,
            )

    class FakeReporter:
        def __init__(self, *args, **kwargs) -> None:
            # The factory hands the evidence provider positionally (ZER-8 S8),
            # so a keyword-only double would reject the real call signature.
            constructions["reporter_args"] = args
            constructions["reporter_kwargs"] = kwargs

    class FakeProxy:
        def __init__(self, **kwargs) -> None:
            constructions["proxy_kwargs"] = kwargs

    class FakeWebSocketHandler:
        def __init__(self, **kwargs) -> None:
            constructions["websocket_kwargs"] = kwargs

    async def fake_build_signer(_settings, configured_secret_provider):
        assert configured_secret_provider is secret_provider
        return signer

    monkeypatch.setattr("zeroth.service.bootstrap.factory.get_settings", lambda: settings)
    monkeypatch.setattr(
        "zeroth.service.bootstrap.factory.build_signing_provider_async", fake_build_signer
    )
    # The verify side is built from the same shared secret provider, and this
    # test hands in a bare sentinel rather than a real one.
    monkeypatch.setattr(
        "zeroth.service.bootstrap.factory.build_verification_provider_async", fake_build_signer
    )
    monkeypatch.setattr("zeroth.service.bootstrap.factory.HTTPGatewayTransport", FakeTransport)
    monkeypatch.setattr("zeroth.service.bootstrap.factory.CompatibilityDetector", FakeDetector)
    monkeypatch.setattr("zeroth.service.bootstrap.factory.CapabilityReporter", FakeReporter)
    monkeypatch.setattr("zeroth.service.bootstrap.factory.GatewayProxy", FakeProxy)
    monkeypatch.setattr(
        "zeroth.service.bootstrap.factory.WebSocketGatewayHandler", FakeWebSocketHandler
    )

    service = await bootstrap_service(
        sqlite_db,
        deployment_ref=deployment.deployment_ref,
        secret_provider=secret_provider,
    )

    proxy_kwargs = constructions["proxy_kwargs"]
    websocket_kwargs = constructions["websocket_kwargs"]
    assert isinstance(proxy_kwargs, dict)
    assert isinstance(websocket_kwargs, dict)
    assert service.langgraph_gateway_transport is not None
    assert service.langgraph_gateway_compatibility.status is CompatibilityStatus.SUPPORTED
    assert proxy_kwargs["transport"] is service.langgraph_gateway_transport
    assert proxy_kwargs["policy_guard"] is service.policy_guard
    assert proxy_kwargs["budget_checker"] is service.budget_enforcer
    assert proxy_kwargs["compatibility"] is service.langgraph_gateway_compatibility
    assert proxy_kwargs["capability_reporter"] is service.langgraph_gateway_capability_reporter
    assert websocket_kwargs["transport"] is service.langgraph_gateway_transport
    assert websocket_kwargs["policy_guard"] is service.policy_guard
    assert websocket_kwargs["budget_checker"] is service.budget_enforcer
    assert constructions["transport_secret_provider"] is service.secret_provider
    assert service.signer is signer


async def test_later_bootstrap_failure_closes_gateway_transport_once(
    sqlite_db, monkeypatch
) -> None:
    from zeroth.platform.config import LangGraphGatewaySettings, get_settings
    from zeroth.platform.signing import EnvHmacSigner

    deployment = await _deploy_test_graph(sqlite_db, "graph-gateway-late-failure")
    gateway_settings = LangGraphGatewaySettings(
        enabled=True,
        upstream_url="http://agent-server.test",
        upstream_audience="agent-server:test",
        deployment_ref=deployment.deployment_ref,
    )
    settings = get_settings().model_copy(update={"langgraph_gateway": gateway_settings})
    signer = EnvHmacSigner(key_id="test", keys={"test": b"gateway-signing-key"})
    closes = 0

    class FakeTransport:
        def __init__(self, _settings, _secret_provider) -> None:
            self.client = SimpleNamespace(base_url=None)

        async def aclose(self) -> None:
            nonlocal closes
            closes += 1

    class FakeDetector:
        def __init__(self, _client, **_kwargs) -> None:
            pass

        async def detect(self) -> CompatibilityResult:
            return CompatibilityResult(
                tested_langgraph_versions=("1.2.9",),
                tested_agent_server_versions=("0.11.1",),
                detected_agent_server_version="0.11.1",
                status=CompatibilityStatus.SUPPORTED,
            )

    async def fake_build_signer(_settings, _secret_provider):
        return signer

    class LaterBootstrapFailure:
        def __init__(self, **_kwargs) -> None:
            raise RuntimeError("later bootstrap construction failed")

    monkeypatch.setattr("zeroth.service.bootstrap.factory.get_settings", lambda: settings)
    monkeypatch.setattr(
        "zeroth.service.bootstrap.factory.build_signing_provider_async", fake_build_signer
    )
    # Same reason as above: no real secret provider is wired in this test.
    monkeypatch.setattr(
        "zeroth.service.bootstrap.factory.build_verification_provider_async", fake_build_signer
    )
    monkeypatch.setattr("zeroth.service.bootstrap.factory.HTTPGatewayTransport", FakeTransport)
    monkeypatch.setattr("zeroth.service.bootstrap.factory.CompatibilityDetector", FakeDetector)
    monkeypatch.setattr("zeroth.service.bootstrap.factory.ServiceBootstrap", LaterBootstrapFailure)

    with pytest.raises(RuntimeError, match="later bootstrap construction failed"):
        await bootstrap_service(
            sqlite_db,
            deployment_ref=deployment.deployment_ref,
            secret_provider=object(),
        )

    assert closes == 1


def test_gateway_transport_closes_once_when_startup_fails() -> None:
    closes = 0

    class FailingWorker:
        async def start(self) -> None:
            raise RuntimeError("startup failed")

    class GatewayTransport:
        async def aclose(self) -> None:
            nonlocal closes
            closes += 1

    bootstrap = SimpleNamespace(
        worker=FailingWorker(),
        langgraph_gateway_transport=GatewayTransport(),
        regulus_client=None,
    )
    app = create_app(bootstrap)

    with pytest.raises(RuntimeError, match="startup failed"), TestClient(app):
        pass

    assert closes == 1


def test_gateway_transport_closes_once_after_successful_lifespan() -> None:
    closes = 0

    class GatewayTransport:
        async def aclose(self) -> None:
            nonlocal closes
            closes += 1

    bootstrap = SimpleNamespace(
        worker=None,
        langgraph_gateway_transport=GatewayTransport(),
        regulus_client=None,
    )
    app = create_app(bootstrap)

    with TestClient(app):
        pass

    assert closes == 1


def _first_matching_route_name(app, method: str, path: str) -> str | None:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "root_path": "",
        "app": app,
    }
    for route in app.router.routes:
        match, _ = route.matches(scope)
        if match is Match.FULL:
            return route.name
    return None


def test_gateway_routes_follow_native_precedence_and_own_agent_server_roots() -> None:
    seen_principal = None

    class Authenticator:
        def authenticate_headers(self, _headers):
            return AuthenticatedPrincipal(
                subject="operator",
                auth_method=AuthMethod.API_KEY,
                roles=[ServiceRole.OPERATOR],
                tenant_id="tenant-a",
            )

    class Proxy:
        async def handle_http(self, request):
            nonlocal seen_principal
            seen_principal = request.state.principal
            return JSONResponse({"upstream": request.url.path})

    deployment = SimpleNamespace(
        deployment_ref="external-agent",
        version=1,
        graph_version_ref="graph:test@1",
    )
    bootstrap = SimpleNamespace(
        deployment=deployment,
        authenticator=Authenticator(),
        audit_repository=None,
        regulus_client=None,
        langgraph_gateway_proxy=Proxy(),
        langgraph_gateway_websocket_handler=object(),
        langgraph_gateway_transport=None,
    )
    app = create_app(bootstrap)

    assert _first_matching_route_name(app, "GET", "/health") == "health"
    assert _first_matching_route_name(app, "POST", "/v1/runs") != "langgraph-gateway"
    for path in ("/threads", "/assistants", "/runs", "/info"):
        assert _first_matching_route_name(app, "GET", path) == "langgraph-gateway"

    with TestClient(app) as client:
        response = client.get("/info", headers={"X-API-Key": "accepted-by-fake"})

    assert response.status_code == 200
    assert response.json() == {"upstream": "/info"}
    assert seen_principal.subject == "operator"


def test_gateway_routes_are_absent_when_disabled() -> None:
    app = create_app(
        SimpleNamespace(
            regulus_client=None,
            langgraph_gateway_proxy=None,
            langgraph_gateway_websocket_handler=None,
            langgraph_gateway_transport=None,
        )
    )

    assert "langgraph-gateway" not in {getattr(route, "name", None) for route in app.router.routes}


def test_gateway_rejects_an_authenticated_principal_without_run_create() -> None:
    proxy_calls = 0

    class Proxy:
        async def handle_http(self, _request):
            nonlocal proxy_calls
            proxy_calls += 1
            return JSONResponse({"proxied": True})

    app = create_app(
        SimpleNamespace(
            audit_repository=None,
            authenticator=ServiceAuthenticator(default_service_auth_config()),
            deployment=None,
            langgraph_gateway_proxy=Proxy(),
            langgraph_gateway_websocket_handler=object(),
            regulus_client=None,
        )
    )

    with TestClient(app) as client:
        response = client.get("/info", headers=reviewer_headers())

    assert response.status_code == 403
    assert response.json() == {"detail": "forbidden"}
    assert proxy_calls == 0


def test_plain_options_requires_authentication_before_gateway_proxy() -> None:
    proxy_calls = 0

    class Proxy:
        async def handle_http(self, _request):
            nonlocal proxy_calls
            proxy_calls += 1
            return JSONResponse({"proxied": True})

    app = create_app(
        SimpleNamespace(
            authenticator=ServiceAuthenticator(ServiceAuthConfig()),
            audit_repository=None,
            regulus_client=None,
            langgraph_gateway_proxy=Proxy(),
            langgraph_gateway_websocket_handler=object(),
            langgraph_gateway_transport=None,
        )
    )

    with TestClient(app) as client:
        response = client.options("/info")

    assert response.status_code == 401
    assert proxy_calls == 0


def test_valid_cors_preflight_is_handled_before_authentication_and_gateway(
    monkeypatch,
) -> None:
    proxy_calls = 0

    class Proxy:
        async def handle_http(self, _request):
            nonlocal proxy_calls
            proxy_calls += 1
            return JSONResponse({"proxied": True})

    monkeypatch.setenv("ZEROTH_CONSOLE_CORS_ORIGINS", "https://console.example")
    app = create_app(
        SimpleNamespace(
            authenticator=ServiceAuthenticator(ServiceAuthConfig()),
            audit_repository=None,
            regulus_client=None,
            langgraph_gateway_proxy=Proxy(),
            langgraph_gateway_websocket_handler=object(),
            langgraph_gateway_transport=None,
        )
    )

    with TestClient(app) as client:
        response = client.options(
            "/info",
            headers={
                "Origin": "https://console.example",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://console.example"
    assert proxy_calls == 0


@pytest.mark.parametrize("path", ["/health-private", "/healthz", "/health/extra"])
def test_only_exact_native_health_paths_bypass_authentication(path: str) -> None:
    proxy_calls = 0

    class Proxy:
        async def handle_http(self, _request):
            nonlocal proxy_calls
            proxy_calls += 1
            return JSONResponse({"proxied": True})

    deployment = SimpleNamespace(
        deployment_ref="external-agent",
        version=1,
        graph_version_ref="graph:test@1",
    )
    app = create_app(
        SimpleNamespace(
            deployment=deployment,
            authenticator=ServiceAuthenticator(ServiceAuthConfig()),
            audit_repository=None,
            regulus_client=None,
            langgraph_gateway_proxy=Proxy(),
            langgraph_gateway_websocket_handler=object(),
            langgraph_gateway_transport=None,
            langgraph_gateway_compatibility=CompatibilityResult(
                tested_langgraph_versions=("1.2.9",),
                tested_agent_server_versions=("0.11.1",),
                detected_agent_server_version="0.11.1",
                status=CompatibilityStatus.SUPPORTED,
            ),
        )
    )

    with TestClient(app) as client:
        native_health = client.get("/health")
        response = client.get(path)

    assert native_health.status_code == 200
    assert response.status_code == 401
    assert proxy_calls == 0
