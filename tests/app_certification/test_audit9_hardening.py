from __future__ import annotations

import json
import os
import signal
import sys
import time
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from release.app_certification import (
    AppDeclaration,
    CertificationRunner,
    CertificationTargets,
    CheckResult,
    CommandResult,
    SmokeSpec,
)
from release.app_certification.candidate_process import run_importer
from release.app_certification.cli import UrlHttpBoundary
from tests.app_certification.test_engine import (
    declaration_data,
    passing_http,
    run_certification,
    write_inputs,
)

WORKFLOW = Path(__file__).parents[2] / ".github/workflows/app-certification.yml"


def _data() -> dict:
    data = declaration_data()
    if "migration_runner" in CertificationTargets.model_fields:
        data["targets"]["migration_runner"] = "zeroth.service.bootstrap:run_migrations"
    return data


def _candidate_result(tmp_path: Path, source: str, check: str) -> CheckResult:
    (tmp_path / "candidate.py").write_text(source, encoding="utf-8")
    data = _data()
    data["targets"]["contracts"] = "candidate:CONTRACTS"
    if check == "graph":
        data["targets"]["graph_builders"] = ["candidate:build_graph"]
    return CertificationRunner(tmp_path, AppDeclaration.model_validate(data))._command(check)


def test_candidate_route_rejects_non_runtime_graph(tmp_path: Path) -> None:
    source = """
from pydantic import BaseModel
from zeroth.contracts.graph import EntrypointNode, Graph
class Payload(BaseModel): value: str
CONTRACTS = {'contract://payload': Payload}
REAL = Graph(graph_id='app', name='App', entry_step='start', nodes=[EntrypointNode(node_id='start', graph_version_ref='app@1', input_contract_ref='contract://payload', output_contract_ref='contract://payload')], edges=[])
class PretendGraph:
    nodes = REAL.nodes
    def model_dump(self, *args, **kwargs): return REAL.model_dump(*args, **kwargs)
def build_graph(): return PretendGraph()
"""
    result = _candidate_result(tmp_path, source, "graph")
    assert result.status == "failed" and "Graph" in result.detail


def test_candidate_route_rejects_non_pydantic_contract(tmp_path: Path) -> None:
    source = """
class PretendContract:
    @classmethod
    def model_json_schema(cls): return {'type': 'object', 'properties': {}}
CONTRACTS = {'contract://payload': PretendContract}
"""
    result = _candidate_result(tmp_path, source, "contracts")
    assert result.status == "failed" and "Pydantic" in result.detail


def test_candidate_route_runs_full_graph_validator_for_custom_reducer(tmp_path: Path) -> None:
    source = """
from pydantic import BaseModel
from zeroth.contracts.graph import AgentNode, AgentNodeData, Graph
from zeroth.runtime.parallel.models import ParallelConfig
class Payload(BaseModel): value: str
CONTRACTS = {'contract://payload': Payload}
def build_graph():
    node = AgentNode(node_id='start', graph_version_ref='app@1', input_contract_ref='contract://payload', output_contract_ref='contract://payload', agent=AgentNodeData(instruction='go', model_provider='test'), parallel_config=ParallelConfig(split_path='items', merge_strategy='custom', reducer_ref='candidate.no_such_reducer'))
    return Graph(graph_id='app', name='App', entry_step='start', nodes=[node], edges=[])
"""
    result = _candidate_result(tmp_path, source, "graph")
    assert result.status == "failed" and "reducer" in result.detail.lower()


def test_declaration_requires_migration_runner() -> None:
    data = _data()
    data["targets"].pop("migration_runner")
    with pytest.raises(ValidationError, match="migration"):
        AppDeclaration.model_validate(data)


def test_production_migration_route_propagates_declared_runner_failure(tmp_path: Path) -> None:
    (tmp_path / "candidate_migration.py").write_text(
        "def run(*args, **kwargs):\n    raise RuntimeError('app migration sentinel')\n",
        encoding="utf-8",
    )
    data = declaration_data()
    data["targets"]["migration_runner"] = "candidate_migration:run"
    declaration = AppDeclaration.model_validate(data)
    declaration_path = tmp_path / "certification.json"
    declaration_path.write_text(json.dumps(data), encoding="utf-8")
    result = CertificationRunner(tmp_path, declaration, declaration_path=declaration_path)._command(
        "migrations"
    )
    assert result.status == "failed" and "app migration sentinel" in result.detail


def test_production_frontend_route_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / "frontend").symlink_to(outside, target_is_directory=True)
    data = _data()
    declaration_path = tmp_path / "certification.json"
    declaration_path.write_text(json.dumps(data), encoding="utf-8")
    result = CertificationRunner(
        tmp_path, AppDeclaration.model_validate(data), declaration_path=declaration_path
    )._command("frontend-api")
    assert result.status == "failed" and "outside" in result.detail.lower()


@pytest.mark.parametrize("check", ["packaged-smoke", "ephemeral-smoke"])
def test_smoke_boundary_rejects_cross_origin_redirect(check: str) -> None:
    visited: list[str] = []

    class Target(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        def do_GET(self) -> None:
            visited.append(self.path)
            body = b'{"status":"accepted","result":{"case":"fixed"}}'
            self.send_response(202)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    target = ThreadingHTTPServer(("127.0.0.1", 0), Target)
    target_thread = __import__("threading").Thread(target=target.serve_forever, daemon=True)
    target_thread.start()

    class Redirect(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        def do_POST(self) -> None:
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target.server_port}/other-origin")
            self.end_headers()

    redirect = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    redirect_thread = __import__("threading").Thread(target=redirect.serve_forever, daemon=True)
    redirect_thread.start()
    smoke = SmokeSpec.model_validate(declaration_data()["smoke"])
    boundary = UrlHttpBoundary(
        f"http://127.0.0.1:{redirect.server_port}",
        f"http://127.0.0.1:{redirect.server_port}",
        {},
    )
    try:
        with pytest.raises(ValueError, match="redirect"):
            boundary(check, smoke)
        assert visited == []
    finally:
        for server, thread in ((redirect, redirect_thread), (target, target_thread)):
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def test_successful_serializer_terminates_background_descendants(tmp_path: Path) -> None:
    pid_path, marker = tmp_path / "child.pid", tmp_path / "descendant-survived"
    child = f"import pathlib,time; time.sleep(.4); pathlib.Path({str(marker)!r}).write_text('alive'); time.sleep(30)"
    parent = (
        "import pathlib,subprocess,sys;"
        f"p=subprocess.Popen([sys.executable,'-c',{child!r}],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
        f"pathlib.Path({str(pid_path)!r}).write_text(str(p.pid))"
    )
    pid = None
    try:
        assert run_importer([sys.executable, "-c", parent])[0] == 0
        pid = int(pid_path.read_text(encoding="utf-8"))
        time.sleep(0.7)
        assert not marker.exists()
    finally:
        if pid is not None:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


def test_workflow_bounds_jobs_containers_and_logs() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert all(
        isinstance(job.get("timeout-minutes"), int) and job["timeout-minutes"] > 0
        for job in workflow["jobs"].values()
    )
    steps = workflow["jobs"]["certify"]["steps"]
    containers = next(step["run"] for step in steps if step.get("id") == "containers")
    for flag in ("--cpus", "--memory", "--pids-limit"):
        assert containers.count(flag) == 2
    assert containers.count("--log-opt max-file=3") == 2
    diagnostics = next(
        step["run"] for step in steps if step["name"] == "Capture container diagnostics"
    )
    log_lines = [line for line in diagnostics.splitlines() if "docker logs" in line]
    assert len(log_lines) == 2 and all("--tail" in line for line in log_lines)


@pytest.mark.parametrize("name", ["packaged-smoke", "ephemeral-smoke", "sbom", "provenance"])
def test_failure_matrix_covers_all_non_host_checks(tmp_path: Path, name: str) -> None:
    write_inputs(tmp_path)
    declaration = AppDeclaration.model_validate(_data())
    if name in {"sbom", "provenance"}:
        (tmp_path / getattr(declaration, f"{name}_path")).unlink()

    def http(check: str, smoke: SmokeSpec):
        return (
            type(passing_http(check, smoke))(500, {})
            if check == name
            else passing_http(check, smoke)
        )

    report = run_certification(tmp_path, declaration, http=http)
    assert report.status == "failed"
    assert next(item for item in report.checks if item.name == name).status == "failed"


@pytest.mark.parametrize(
    ("name", "candidate_route"),
    [("graph", True), ("contracts", True), ("migrations", True), ("frontend-api", False)],
)
def test_semantic_negative_paths_use_production_command_route(
    tmp_path: Path, name: str, candidate_route: bool
) -> None:
    trusted: list[str] = []
    candidate: list[str] = []

    def failing(calls: list[str]):
        def execute(argv: list[str], cwd: Path) -> CommandResult:
            calls.append(name)
            return CommandResult(17, "", "semantic sentinel")

        return execute

    runner = CertificationRunner(
        tmp_path,
        AppDeclaration.model_validate(_data()),
        executor=failing(trusted),
        candidate_executor=failing(candidate),
    )
    result = runner._command(name)
    assert result.status == "failed" and "semantic sentinel" in result.detail
    assert (candidate, trusted) == (([name], []) if candidate_route else ([], [name]))
