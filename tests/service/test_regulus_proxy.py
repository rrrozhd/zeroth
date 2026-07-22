from __future__ import annotations

import json

import httpx
import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.testclient import TestClient

from zeroth.governance.identity import AuthMethod, AuthenticatedPrincipal, ServiceRole
from zeroth.service.api.regulus_proxy_api import ROUTES, register_regulus_proxy_routes


def _app(*, with_backend: bool = True, with_credentials: bool = True) -> FastAPI:
    app = FastAPI()
    app.state.bootstrap = type("Bootstrap", (), {"audit_repository": None, "deployment": None})()
    if with_backend:
        app.state.regulus_base_url = "https://regulus.invalid/v1"
    if with_credentials:
        app.state.regulus_self_auth_headers = lambda: {"Authorization": "Bearer private-token"}

    async def upstream(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/redirect"):
            return httpx.Response(307, headers={"location": "https://evil.invalid"})
        return httpx.Response(
            200,
            json={
                "method": request.method,
                "path": request.url.path,
                "body": json.loads(request.content) if request.content else None,
            },
        )

    app.state.regulus_transport = httpx.MockTransport(upstream)

    @app.middleware("http")
    async def principal(request: Request, call_next):
        role = ServiceRole(request.headers.get("X-Test-Role", ServiceRole.OPERATOR.value))
        request.state.principal = AuthenticatedPrincipal(
            subject="test",
            auth_method=AuthMethod.API_KEY,
            roles=[role],
            tenant_id=request.headers.get("X-Test-Tenant", "default"),
        )
        return await call_next(request)

    router = APIRouter(prefix="/v1")
    register_regulus_proxy_routes(router)
    app.include_router(router)
    return app


PLATFORM = {"X-Test-Role": ServiceRole.PLATFORM_ADMIN.value}


@pytest.mark.parametrize("role", [ServiceRole.OPERATOR, ServiceRole.REVIEWER, ServiceRole.ADMIN])
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/econ/regulus/dashboard/kpis"),
        ("POST", "/v1/econ/regulus/enforcement/actions/12/approve"),
        ("POST", "/v1/econ/regulus/enforcement/actions/12/reject"),
    ],
)
def test_only_platform_admin_can_reach_regulus(role: ServiceRole, method: str, path: str) -> None:
    response = TestClient(_app()).request(
        method,
        path,
        headers={"X-Test-Role": role.value, "X-Test-Tenant": "foreign"},
        json={"reason": "reviewed"} if method == "POST" else None,
    )
    assert response.status_code == 403


@pytest.mark.parametrize("route", ROUTES, ids=lambda route: route.name)
def test_every_declared_route_forwards_its_canonical_method_and_path(route) -> None:
    path = route.console_path.replace("{capability_id}", "cap-1")
    path = path.replace("{implementation_id}", "impl-1").replace("{profile_id}", "profile-1")
    path = path.replace("{action_id}", "12")
    response = TestClient(_app()).request(
        route.method,
        f"/v1/econ/regulus{path}",
        headers=PLATFORM,
        json={"reason": "reviewed"} if route.method == "POST" else None,
    )
    assert response.status_code == 200, response.text
    assert response.json()["method"] == route.method
    assert response.json()["path"] == f"/v1{path}"


@pytest.mark.parametrize(
    "path",
    [
        "/v1/econ/regulus//dashboard/kpis",
        "/v1/econ/regulus/dashboard/../kpis",
        "/v1/econ/regulus/dashboard/%2e%2e/kpis",
        "/v1/econ/regulus/registry/capabilities/a%2fb",
        "/v1/econ/regulus/registry/capabilities/a%5cb",
        "/v1/econ/regulus/dashboard/kpis?unexpected=1",
        "/v1/econ/regulus/auth/token",
    ],
)
def test_noncanonical_or_unknown_paths_never_forward(path: str) -> None:
    response = TestClient(_app()).get(path, headers=PLATFORM)
    assert response.status_code in {400, 404, 405}


@pytest.mark.parametrize("body", [{}, {"reason": "ok", "extra": True}, {"reason": "x" * 2001}])
def test_action_body_is_strict_and_bounded(body: dict[str, object]) -> None:
    response = TestClient(_app()).post(
        "/v1/econ/regulus/enforcement/actions/12/approve", headers=PLATFORM, json=body
    )
    assert response.status_code == 422


def test_action_rejects_invalid_json_and_oversized_raw_body() -> None:
    client = TestClient(_app())
    invalid = client.post(
        "/v1/econ/regulus/enforcement/actions/12/approve",
        headers={**PLATFORM, "Content-Type": "application/json"},
        content=b"{",
    )
    oversized = client.post(
        "/v1/econ/regulus/enforcement/actions/12/approve",
        headers={**PLATFORM, "Content-Type": "application/json"},
        content=b'{' + b'"reason":"' + b"x" * 8192 + b'"}',
    )
    assert invalid.status_code == 422
    assert oversized.status_code == 413


@pytest.mark.parametrize("kwargs", [{"with_backend": False}, {"with_credentials": False}])
def test_missing_backend_or_credentials_is_stable_503(kwargs: dict[str, bool]) -> None:
    response = TestClient(_app(**kwargs)).get(
        "/v1/econ/regulus/dashboard/kpis", headers=PLATFORM
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "Regulus backend unavailable"}


@pytest.mark.parametrize("status_code", [307, 500])
def test_redirects_and_upstream_failures_are_sanitized(status_code: int) -> None:
    app = _app()

    def failing(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            text="secret upstream response",
            headers={"location": "https://token.example.invalid/private"},
        )

    app.state.regulus_transport = httpx.MockTransport(failing)
    response = TestClient(app).get("/v1/econ/regulus/dashboard/kpis", headers=PLATFORM)

    assert response.status_code == 502
    assert response.json() == {"detail": "Regulus backend request failed"}
    assert "secret" not in response.text
    assert "token.example" not in response.text
