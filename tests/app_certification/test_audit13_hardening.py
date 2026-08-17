from __future__ import annotations

import json
import shutil
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
import yaml
from psycopg import sql

from release.app_certification import AppDeclaration, CertificationRunner
from release.app_certification.checks import validate_serialized_service_config
from release.app_certification.cli import _parser, _untrusted_executor
from release.app_certification.runner import CommandResult
from tests.app_certification.test_engine import declaration_data, write_semantic_inputs
from tests.conftest import requires_docker


ROOT = Path(__file__).parents[2]
WORKFLOW = ROOT / ".github/workflows/app-certification.yml"


def _step(step_id: str) -> str:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return next(
        step["run"] for step in workflow["jobs"]["certify"]["steps"] if step.get("id") == step_id
    )


def _handoff_arguments() -> list[str]:
    return [
        "--report",
        "report.json",
        "--root",
        "root",
        "--workflow-evidence",
        "workflow-evidence.json",
        "--workflow-stages",
        "workflow-stages.json",
        "--cleanup",
        "cleanup.json",
        "--image-archive",
        "image.tar",
        "--source-archive",
        "source.tar",
        "--app-repository",
        "app",
        "--app-commit",
        "a" * 40,
        "--zeroth-version",
        "0.23.9.15",
        "--zeroth-commit",
        "b" * 40,
        "--certifier-wheel",
        "zeroth.whl",
        "--requirements-lock",
        "requirements.txt",
        "--wheel-installation",
        "installed-wheel.json",
        "--verdict",
        "verdict.json",
    ]


@pytest.mark.parametrize(
    "argv",
    [
        [
            "verify-wheel-installation",
            "--wheel",
            "zeroth.whl",
            "--site-packages",
            "site-packages",
            "--image-digest",
            "sha256:" + "c" * 64,
            "--output",
            "installed-wheel.json",
        ],
        [
            "prepare-evidence",
            "--declaration",
            "certification.json",
            "--root",
            ".",
            "--app-commit",
            "a" * 40,
            "--image-digest",
            "sha256:" + "c" * 64,
            "--source-digest",
            "sha256:" + "d" * 64,
            "--raw-sbom",
            "image.spdx.json",
            "--zeroth-commit",
            "b" * 40,
            "--certifier-wheel",
            "zeroth.whl",
            "--requirements-lock",
            "requirements.txt",
            "--wheel-installation",
            "installed-wheel.json",
        ],
        ["validate-handoff", *_handoff_arguments()],
        [
            "finalize-attestation",
            "--bundle",
            "bundle.json",
            "--repository",
            "owner/app",
            "--signer-repo",
            "owner/zeroth",
            "--signer-workflow",
            "owner/zeroth/.github/workflows/app-certification.yml",
            "--signer-digest",
            "b" * 40,
            *_handoff_arguments(),
        ],
    ],
    ids=["wheel-proof", "evidence", "handoff", "attestation"],
)
def test_current_proof_routes_require_image_configuration(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        _parser().parse_args(argv)


def test_exact_wheel_and_command_cannot_select_a_replaced_candidate_runtime() -> None:
    image = _step("image")
    wheel = _step("wheel")
    containers = _step("containers")

    assert "CANDIDATE_IMAGE_REFERENCE" in image
    assert "prepare-runtime-context" in image
    assert "Dockerfile.certification-runtime" in image
    assert "verify-wheel-installation" in wheel
    assert '--image-config "$HANDOFF_ROOT/image-config.json"' in wheel
    assert "--entrypoint /usr/local/bin/python" not in containers
    assert "run-certified-runtime" not in containers


def _replaced_runtime_inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    from release.app_certification import wheel_installation

    source = tmp_path / "source"
    wheel = tmp_path / "zeroth_core-0.23.9.15-py3-none-any.whl"
    requirements = tmp_path / "requirements-image.txt"
    candidate_root = tmp_path / "candidate-root"
    source.mkdir()
    wheel.write_bytes(b"exact trusted wheel")
    requirements.write_text("exact-dependency==1 --hash=sha256:abc\n", encoding="utf-8")
    (source / "candidate_app.py").write_text("print('real app')\n", encoding="utf-8")
    fake_python = candidate_root / "usr/local/bin/python"
    fake_python.parent.mkdir(parents=True)
    fake_python.write_text("#!/bin/sh\necho synthetic-server\n", encoding="utf-8")
    fake_loader = candidate_root / "lib/x86_64-linux-gnu/ld-linux-x86-64.so.2"
    fake_loader.parent.mkdir(parents=True)
    fake_loader.write_bytes(b"candidate loader")
    config = tmp_path / "candidate-config.json"
    config.write_text(
        json.dumps(
            {
                "Cmd": [
                    "/usr/local/bin/python",
                    "-I",
                    "-S",
                    "-c",
                    wheel_installation.RUNTIME_BOOTSTRAP,
                    "run-certified-runtime",
                    "/usr/local/lib/python3.12/site-packages",
                    "/opt/app",
                    "candidate_app",
                ],
                "Entrypoint": None,
                "Env": [
                    "LD_PRELOAD=/candidate/libfake.so",
                    "GPG_KEY=candidate-controlled",
                    "APP_MODE=certification",
                ],
            }
        ),
        encoding="utf-8",
    )
    return source, wheel, requirements, config, fake_loader


def test_replaced_candidate_runtime_is_excluded_from_certifier_context(tmp_path: Path) -> None:
    from release.app_certification import wheel_installation

    prepare = getattr(wheel_installation, "prepare_runtime_context", None)
    trusted_image = getattr(wheel_installation, "TRUSTED_RUNTIME_IMAGE", "")
    assert callable(prepare)
    assert trusted_image.startswith("python:3.12.13-slim-bookworm@sha256:")
    source, wheel, requirements, config, fake_loader = _replaced_runtime_inputs(tmp_path)
    context = tmp_path / "runtime-context"

    prepare(source, wheel, requirements, config, context)

    dockerfile = (context / "Dockerfile.certification-runtime").read_text(encoding="utf-8")
    assert dockerfile.startswith(f"FROM {trusted_image}\n")
    assert (
        "COPY zeroth_core-0.23.9.15-py3-none-any.whl "
        "/opt/zeroth/zeroth_core-0.23.9.15-py3-none-any.whl"
    ) in dockerfile
    assert "/opt/zeroth/zeroth_core-0.23.9.15-py3-none-any.whl" in dockerfile
    assert "COPY requirements-image.txt /tmp/requirements-image.txt" in dockerfile
    assert "COPY app/ /opt/app/" in dockerfile
    assert "LD_PRELOAD" not in dockerfile
    assert "candidate-controlled" not in dockerfile
    assert "APP_MODE" in dockerfile
    assert (context / wheel.name).read_bytes() == b"exact trusted wheel"
    assert not any(path.name == "python" for path in context.rglob("python"))
    assert not any(path.name == fake_loader.name for path in context.rglob(fake_loader.name))


def _candidate_root(tmp_path: Path, migration_source: str | None = None) -> CertificationRunner:
    shutil.copytree(ROOT / "apps/vendor_dd", tmp_path / "apps/vendor_dd")
    if migration_source is not None:
        (tmp_path / "apps/vendor_dd/migrations.py").write_text(migration_source, encoding="utf-8")
    data = declaration_data()
    declaration = AppDeclaration.model_validate(data)
    declaration_path = tmp_path / "certification.json"
    declaration_path.write_text(json.dumps(data), encoding="utf-8")
    return CertificationRunner(
        tmp_path,
        declaration,
        declaration_path=declaration_path,
        check_python=Path(sys.executable),
    )


def _fresh_postgres_dsn(postgres_container) -> str:
    base = postgres_container.get_connection_url().replace(
        "postgresql+psycopg2://", "postgresql://"
    )
    database = f"certifier_{uuid.uuid4().hex}"
    with psycopg.connect(base, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    parts = urlsplit(base)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", "", ""))


def test_service_finalizer_accepts_canonical_postgres_backend() -> None:
    from apps.vendor_dd.entrypoint import build_auth_config

    validate_serialized_service_config(
        {
            "auth_config": build_auth_config().model_dump(mode="json"),
            "database_backend": "postgres",
        }
    )


def test_contained_candidate_receives_only_selected_database_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from release.app_certification import cli

    captured: list[str] = []

    def execute(argv: list[str], cwd: Path) -> CommandResult:
        assert cwd == tmp_path
        captured.extend(argv)
        return CommandResult(0, "", "")

    monkeypatch.setenv("ZEROTH_DATABASE__BACKEND", "postgres")
    monkeypatch.setenv("ZEROTH_DATABASE__POSTGRES_DSN", "postgresql://selected/fresh")
    monkeypatch.setenv("LD_PRELOAD", "/candidate/replaced-loader.so")
    monkeypatch.setattr(cli, "execute_command", execute)

    _untrusted_executor("candidate")(["python", "check.py"], tmp_path)

    assert "ZEROTH_DATABASE__BACKEND=postgres" in captured
    assert "ZEROTH_DATABASE__POSTGRES_DSN=postgresql://selected/fresh" in captured
    assert not any("LD_PRELOAD" in argument for argument in captured)


@requires_docker
def test_broken_postgres_migration_fails_through_production_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, postgres_container
) -> None:
    dsn = _fresh_postgres_dsn(postgres_container)
    monkeypatch.setenv("ZEROTH_DATABASE__BACKEND", "postgres")
    monkeypatch.setenv("ZEROTH_DATABASE__POSTGRES_DSN", dsn)
    runner = _candidate_root(
        tmp_path,
        """
from zeroth.service.bootstrap import run_migrations

def migrate(database_url: str) -> None:
    if database_url.startswith("postgresql"):
        raise RuntimeError("broken PostgreSQL migration sentinel")
    run_migrations(database_url)
""",
    )

    result = runner._command("migrations")

    assert result.status == "failed"
    assert "broken PostgreSQL migration sentinel" in result.detail


@requires_docker
def test_real_postgres_service_and_fresh_migration_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, postgres_container
) -> None:
    dsn = _fresh_postgres_dsn(postgres_container)
    monkeypatch.setenv("ZEROTH_DATABASE__BACKEND", "postgres")
    monkeypatch.setenv("ZEROTH_DATABASE__POSTGRES_DSN", dsn)
    runner = _candidate_root(tmp_path)

    service = runner._command("service-config")
    migration = runner._command("migrations")

    assert service.status == "passed", service.detail
    assert migration.status == "passed", migration.detail
    with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
        )
        assert cursor.fetchone()[0] > 0


def _custom_reducer_result(tmp_path: Path, reducer_definition: str, reducer_ref: str):
    (tmp_path / "candidate.py").write_text(
        f"""
from pydantic import BaseModel
from zeroth.contracts.graph import AgentNode, AgentNodeData, Graph
from zeroth.runtime.parallel.models import ParallelConfig

class Payload(BaseModel):
    value: str

CONTRACTS = {{'contract://payload': Payload}}
{reducer_definition}

def build_graph():
    node = AgentNode(
        node_id='start',
        graph_version_ref='app@1',
        input_contract_ref='contract://payload',
        output_contract_ref='contract://payload',
        agent=AgentNodeData(instruction='go', model_provider='test'),
        parallel_config=ParallelConfig(
            split_path='items',
            merge_strategy='custom',
            reducer_ref={reducer_ref!r},
        ),
    )
    return Graph(
        graph_id='app', name='App', entry_step='start', nodes=[node], edges=[]
    )
""",
        encoding="utf-8",
    )
    data = declaration_data()
    data["targets"]["graph_builders"] = ["candidate:build_graph"]
    data["targets"]["contracts"] = "candidate:CONTRACTS"
    declaration = write_semantic_inputs(tmp_path, data, updates={"reducers": [reducer_ref]})
    return CertificationRunner(tmp_path, declaration, check_python=Path(sys.executable))._command(
        "graph"
    )


def test_app_local_callable_reducer_is_outside_static_certification_contract(
    tmp_path: Path,
) -> None:
    result = _custom_reducer_result(
        tmp_path,
        "def merge_values(left, right): return right",
        "candidate.merge_values",
    )

    assert result.status == "failed"
    assert "outside the static certification contract" in result.detail


@pytest.mark.parametrize(
    ("definition", "reference"),
    [
        ("", "candidate.missing_reducer"),
        ("merge_values = 3", "candidate.merge_values"),
    ],
    ids=["missing", "non-callable"],
)
def test_app_local_invalid_reducer_fails_contained_candidate_validation(
    tmp_path: Path, definition: str, reference: str
) -> None:
    result = _custom_reducer_result(tmp_path, definition, reference)

    assert result.status == "failed"
    assert "reducer" in result.detail.lower()
