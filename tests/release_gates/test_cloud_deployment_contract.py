from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_cloud_image_exposes_the_narrow_economic_plane_at_the_public_root() -> None:
    dockerfile = (ROOT / "Dockerfile.cloud").read_text(encoding="utf-8")
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert (
        'CMD ["uvicorn", "zeroth.econ.plane.main:app", "--host", "0.0.0.0", '
        '"--port", "8000"]'
    ) in dockerfile
    assert "zeroth-core serve" not in dockerfile
    assert "ZEROTH_DATABASE__" not in dockerfile
    assert "ZEROTH_REDIS__" not in dockerfile
    assert "ZEROTH_AUTO_AGENT_RUNNERS" not in dockerfile
    assert ".railway/" in gitignore


def test_cloud_plane_owns_the_documented_root_health_signup_and_sdk_paths() -> None:
    from zeroth.econ.plane.main import app

    paths = app.openapi()["paths"]
    route_paths = {getattr(route, "path", None) for route in app.routes}
    assert "/" in route_paths
    assert "/account" in route_paths
    assert "/health/ready" in paths
    assert "/v1/cloud/auth/login" in paths
    assert "/v1/cloud/auth/callback" in paths
    assert "/v1/cloud/billing/checkout" in paths
    assert "/v1/cloud/billing/portal" in paths
    assert "/v1/backtests" in paths
