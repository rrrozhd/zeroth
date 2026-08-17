from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from release.app_certification.cli import main as certification_main
from release.app_certification.wheel_installation import RUNTIME_BOOTSTRAP
from tests.app_certification.test_audit11_hardening import _wheel_fixture
from tests.app_certification.test_engine import declaration_data


ROOT = Path(__file__).parents[2]
WORKFLOW = ROOT / ".github/workflows/app-certification.yml"


def test_declared_dockerfile_must_resolve_inside_build_context(tmp_path: Path) -> None:
    context = tmp_path / "build-context"
    generated = tmp_path / "generated"
    context.mkdir()
    generated.mkdir()
    (generated / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (context / "Dockerfile.certification").symlink_to("../generated/Dockerfile")
    declaration = context / "certification.json"
    declaration.write_text(json.dumps(declaration_data()), encoding="utf-8")

    result = certification_main(["validate-declaration", "--declaration", str(declaration)])

    assert result == 2

    (context / "generated.Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (context / "Dockerfile.certification").unlink()
    (context / "Dockerfile.certification").symlink_to("generated.Dockerfile")
    assert certification_main(["validate-declaration", "--declaration", str(declaration)]) == 2


def test_workflow_binds_dockerfile_to_build_context() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    prepare = next(
        step["run"] for step in workflow["jobs"]["certify"]["steps"] if step.get("id") == "prepare"
    )

    assert "--root build-context" in prepare
    assert 'realpath -e "build-context/$DOCKERFILE"' in prepare


def _runtime_paths(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    site_packages = tmp_path / "site-packages"
    shadow = tmp_path / "shadow"
    app_root = tmp_path / "app"
    marker = tmp_path / "runtime-origin"
    for root, origin in ((site_packages, "verified-wheel"), (shadow, "shadow")):
        package = root / "zeroth"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text(f"RUNTIME_ORIGIN = {origin!r}\n", encoding="utf-8")
    (site_packages / "candidate_runtime.py").write_text(
        "import os\nfrom pathlib import Path\n"
        "Path(os.environ['RUNTIME_ORIGIN_MARKER']).write_text("
        "'site-shadow', encoding='utf-8')\n",
        encoding="utf-8",
    )
    (app_root / "zeroth").mkdir(parents=True)
    (app_root / "zeroth/__init__.py").write_text(
        "RUNTIME_ORIGIN = 'app-shadow'\n", encoding="utf-8"
    )
    (app_root / "candidate_runtime.py").write_text(
        "import os\nfrom pathlib import Path\nimport zeroth\n"
        "Path(os.environ['RUNTIME_ORIGIN_MARKER']).write_text("
        "zeroth.RUNTIME_ORIGIN, encoding='utf-8')\n",
        encoding="utf-8",
    )
    return site_packages, shadow, app_root, marker


def test_runtime_identity_imports_zeroth_from_verified_site_packages(
    tmp_path: Path,
) -> None:
    site_packages, shadow, app_root, marker = _runtime_paths(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            RUNTIME_BOOTSTRAP,
            "run-certified-runtime",
            str(site_packages),
            str(app_root),
            "candidate_runtime",
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "PYTHONPATH": str(shadow),
            "RUNTIME_ORIGIN_MARKER": str(marker),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert marker.read_text(encoding="utf-8") == "verified-wheel"


def test_runtime_identity_starts_both_candidates_through_verified_command() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    wheel = next(
        step["run"] for step in workflow["jobs"]["certify"]["steps"] if step.get("id") == "wheel"
    )
    containers = next(
        step["run"]
        for step in workflow["jobs"]["certify"]["steps"]
        if step.get("id") == "containers"
    )

    assert "--image-config app/.app-certification/image-config.json" in wheel
    assert '--image-digest "$IMAGE_DIGEST"' in wheel
    assert containers.count("--entrypoint /usr/local/bin/python") == 2
    assert containers.count("run-certified-runtime") == 2
    assert containers.count("PYTHONSAFEPATH=1") == 2


def test_runtime_identity_reference_keeps_the_verified_interpreter(monkeypatch) -> None:
    from apps.vendor_dd import certification_entrypoint

    async def seed_main() -> int:
        return 0

    monkeypatch.setenv("APP_CERTIFICATION_API_KEY", "test-key")
    seed = ModuleType("apps.vendor_dd.seed")
    service = ModuleType("apps.vendor_dd.entrypoint")
    seed.main = seed_main  # type: ignore[attr-defined]
    service.main = lambda: 0  # type: ignore[attr-defined]
    modules = {"apps.vendor_dd.seed": seed, "apps.vendor_dd.entrypoint": service}
    monkeypatch.setattr(
        certification_entrypoint,
        "import_module",
        modules.__getitem__,
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("certified runtime spawned a new interpreter"),
    )
    monkeypatch.setattr(
        certification_entrypoint.os,
        "execv",
        lambda *args: pytest.fail("certified runtime replaced the verified interpreter"),
    )

    assert certification_entrypoint.main() == 0


def test_runtime_identity_wheel_proof_binds_absolute_isolated_command(
    tmp_path: Path,
) -> None:
    wheel, site_packages = _wheel_fixture(tmp_path)
    image_config = tmp_path / "image-config.json"
    output = tmp_path / "installed-wheel.json"
    image_digest = "sha256:" + "a" * 64
    command = [
        "/usr/local/bin/python",
        "-I",
        "-S",
        "-c",
        RUNTIME_BOOTSTRAP,
        "run-certified-runtime",
        "/usr/local/lib/python3.12/site-packages",
        "/opt/app",
        "candidate.entrypoint",
    ]
    image_config.write_text(json.dumps({"Cmd": command, "Entrypoint": None}), encoding="utf-8")
    argv = [
        "verify-wheel-installation",
        "--wheel",
        str(wheel),
        "--site-packages",
        str(site_packages),
        "--image-config",
        str(image_config),
        "--image-digest",
        image_digest,
        "--output",
        str(output),
    ]

    assert certification_main(argv) == 0
    proof = json.loads(output.read_text(encoding="utf-8"))
    assert proof["runtime"]["image_digest"] == image_digest
    assert proof["runtime"]["site_packages"] == command[6]

    command[0] = "python"
    image_config.write_text(json.dumps({"Cmd": command, "Entrypoint": None}), encoding="utf-8")
    assert certification_main(argv) == 2
