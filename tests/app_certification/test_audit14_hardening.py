from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
import yaml
from psycopg import sql

from release.app_certification import AppDeclaration, CertificationRunner
from tests.app_certification.test_engine import declaration_data, write_semantic_inputs
from tests.conftest import requires_docker


ROOT = Path(__file__).parents[2]
WORKFLOW = ROOT / ".github/workflows/app-certification.yml"


def _recovery(mode: str) -> str:
    if mode == "frame":
        return (
            "frame = sys._getframe()\n"
            "owned = None\n"
            "while frame is not None:\n"
            "    if 'collect_candidate_evidence' in frame.f_globals:\n"
            "        owned = frame.f_globals\n"
            "        break\n"
            "    frame = frame.f_back\n"
        )
    return (
        "owned = next((obj.__globals__ for obj in gc.get_objects() "
        "if isinstance(obj, types.FunctionType) "
        "and obj.__name__ == 'collect_candidate_evidence' "
        "and '_load_target' in obj.__globals__), None)\n"
    )


def _runner(
    root: Path, data: dict, *, semantic_updates: dict | None = None
) -> CertificationRunner:
    return CertificationRunner(
        root,
        write_semantic_inputs(root, data, updates=semantic_updates),
        check_python=Path(sys.executable),
    )


@pytest.mark.parametrize("recovery", ["frame", "gc"])
def test_candidate_cannot_skip_invalid_reducer_resolution(tmp_path: Path, recovery: str) -> None:
    (tmp_path / "candidate.py").write_text(
        "import gc, sys, types\n"
        "from pydantic import BaseModel\n"
        "from zeroth.contracts.graph import AgentNode, AgentNodeData, Graph\n"
        "from zeroth.runtime.parallel.models import ParallelConfig\n"
        "class Payload(BaseModel):\n    value: str\n"
        "CONTRACTS = {'contract://payload': Payload}\n"
        + _recovery(recovery)
        + "if owned is not None:\n"
        "    owned['resolve_reducer_ref'] = lambda reference: None\n"
        "def build_graph():\n"
        "    node = AgentNode(node_id='start', graph_version_ref='app@1', "
        "input_contract_ref='contract://payload', output_contract_ref='contract://payload', "
        "agent=AgentNodeData(instruction='go', model_provider='test'), "
        "parallel_config=ParallelConfig(split_path='items', merge_strategy='custom', "
        "reducer_ref='candidate.missing_reducer'))\n"
        "    return Graph(graph_id='app', name='App', entry_step='start', "
        "nodes=[node], edges=[])\n",
        encoding="utf-8",
    )
    data = declaration_data()
    data["targets"]["graph_builders"] = ["candidate:build_graph"]
    data["targets"]["contracts"] = "candidate:CONTRACTS"

    result = _runner(
        tmp_path,
        data,
        semantic_updates={"reducers": ["candidate.missing_reducer"]},
    )._command("graph")

    assert result.status == "failed"
    assert "reducer" in result.detail.lower()


def _fresh_postgres_dsn(postgres_container) -> str:
    base = postgres_container.get_connection_url().replace(
        "postgresql+psycopg2://", "postgresql://"
    )
    database = f"audit14_{uuid.uuid4().hex}"
    with psycopg.connect(base, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    parts = urlsplit(base)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", "", ""))


@requires_docker
@pytest.mark.parametrize("recovery", ["frame", "gc"])
def test_candidate_cannot_forge_noop_postgres_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    postgres_container,
    recovery: str,
) -> None:
    (tmp_path / "candidate.py").write_text(
        "import gc, sys, types\n"
        + _recovery(recovery)
        + "responses = iter(([], ['public.forged']))\n"
        "if owned is not None:\n"
        "    owned['_postgres_tables'] = lambda database_url: next(responses)\n"
        "def migrate(database_url):\n    pass\n",
        encoding="utf-8",
    )
    dsn = _fresh_postgres_dsn(postgres_container)
    monkeypatch.setenv("ZEROTH_DATABASE__BACKEND", "postgres")
    monkeypatch.setenv("ZEROTH_DATABASE__POSTGRES_DSN", dsn)
    data = declaration_data()
    data["targets"]["migration_runner"] = "candidate:migrate"

    result = _runner(tmp_path, data)._command("migrations")

    assert result.status == "failed"
    assert "did not create postgresql tables" in result.detail.lower()


def _write_import_attack(root: Path, recovery: str) -> None:
    (root / "candidate.py").write_text(
        "import gc, sys, types\n" + _recovery(recovery) + "if owned is not None:\n"
        "    owned['_load_target'] = lambda reference: object()\n"
        "def build_graph():\n    return object()\n",
        encoding="utf-8",
    )
    for name in ("contracts", "auth", "policy", "migration"):
        (root / f"invalid_{name}.py").write_text(
            f"raise RuntimeError('invalid {name} import executed')\n",
            encoding="utf-8",
        )


@pytest.mark.parametrize("recovery", ["frame", "gc"])
def test_static_optional_contract_never_imports_candidate_targets(
    tmp_path: Path, recovery: str
) -> None:
    _write_import_attack(tmp_path, recovery)
    data = declaration_data()
    data["zeroth_version"] = __import__("importlib.metadata").metadata.version("zeroth-core")
    data["targets"].update(
        {
            "graph_builders": ["candidate:build_graph"],
            "contracts": "invalid_contracts:CONTRACTS",
            "auth_config": "invalid_auth:build_auth_config",
            "policy_guard": "invalid_policy:build_policy_guard",
            "migration_runner": "invalid_migration:migrate",
        }
    )

    result = _runner(tmp_path, data)._command("optional-extras")

    assert result.status == "passed", result.detail


def test_committed_reserved_symlink_cannot_block_report_or_upload(tmp_path: Path) -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["certify"]
    finalizer = next(
        step for step in job["steps"] if step["name"].startswith("Finalize canonical report")
    )
    upload = next(
        step for step in job["steps"] if step["name"] == "Upload unprivileged certification handoff"
    )
    app = tmp_path / "app"
    blocked = tmp_path / "candidate-controlled"
    handoff = tmp_path / "certifier-owned-handoff"
    app.mkdir()
    blocked.mkdir()
    blocked.chmod(0o500)
    (app / ".app-certification").symlink_to(blocked, target_is_directory=True)
    fallback = finalizer["run"].split("|| ", 1)[1]
    try:
        result = subprocess.run(
            shlex.split(fallback),
            cwd=tmp_path,
            env={
                **os.environ,
                "HANDOFF_ROOT": str(handoff),
                "APP_CHECKOUT": "success",
                "CERTIFIER_CHECKOUT": "success",
                "PREPARE": "failure",
            },
            capture_output=True,
            text=True,
        )
    finally:
        blocked.chmod(0o700)

    assert result.returncode == 0, result.stderr
    assert json.loads((handoff / "report.json").read_text())["status"] == "failed"
    assert job["env"]["HANDOFF_ROOT"].startswith("${{ runner.temp }}")
    assert '--root "$HANDOFF_ROOT"' in finalizer["run"]
    assert upload["with"]["path"] == "${{ env.HANDOFF_ROOT }}"
