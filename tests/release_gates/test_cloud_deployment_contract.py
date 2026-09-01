from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_cloud_image_exposes_the_narrow_economic_plane_at_the_public_root() -> None:
    dockerfile = (ROOT / "Dockerfile.cloud").read_text(encoding="utf-8")
    railway = (ROOT / ".railway" / "railway.ts").read_text(encoding="utf-8")

    command = "uvicorn zeroth.econ.plane.main:app --host 0.0.0.0 --port 8000"
    assert (
        'CMD ["uvicorn", "zeroth.econ.plane.main:app", "--host", "0.0.0.0", '
        '"--port", "8000"]'
    ) in dockerfile
    assert f'startCommand: "{command}"' in railway
    assert "zeroth-core serve" not in dockerfile
    assert "zeroth-core serve" not in railway
    assert "ZEROTH_DATABASE__" not in dockerfile
    assert "ZEROTH_REDIS__" not in dockerfile
    assert "ZEROTH_AUTO_AGENT_RUNNERS" not in dockerfile
    assert 'preDeployCommand: ["zeroth-core migrate-econ"]' in railway
    assert "ZEROTH_DATABASE__" not in railway
    assert "ZEROTH_REDIS__" not in railway
    assert "ZEROTH_AUTO_AGENT_RUNNERS" not in railway
    assert "ECP_DATABASE_URL: database.env.DATABASE_URL" in railway
    assert 'ECP_CLOUD_SCHEDULER_ENABLED: "true"' in railway


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
