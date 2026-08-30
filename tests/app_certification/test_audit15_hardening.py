from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import os
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest
import yaml

from release.app_certification import AppDeclaration, CertificationRunner, validate_source_archive
from tests.app_certification.test_engine import declaration_data, write_semantic_inputs
from tests.app_certification.test_hardening import identity


ROOT = Path(__file__).parents[2]
WORKFLOW = ROOT / ".github/workflows/app-certification.yml"


def _descriptor_forgery(payload: str) -> str:
    return (
        f"payload = {payload}\n"
        "encoded = (json.dumps(payload, sort_keys=True, separators=(',', ':')) + '\\n').encode()\n"
        "for descriptor in range(3, 256):\n"
        "    try:\n"
        "        os.write(descriptor, encoded)\n"
        "    except OSError:\n"
        "        pass\n"
        "os._exit(0)\n"
    )


def test_descriptor_scan_has_no_candidate_serialization_channel(tmp_path: Path) -> None:
    source = tmp_path / "candidate_attack.py"
    source.write_text(
        "import hashlib, json, os\n"
        "source_digest = 'sha256:' + hashlib.sha256(open(__file__, 'rb').read()).hexdigest()\n"
        + _descriptor_forgery(
            "{'check': 'contracts', 'evidence': {'contracts': {'contract://payload': "
            "{'properties': {'value': {'title': 'Value', 'type': 'string'}}, "
            "'required': ['value'], 'title': 'Payload', 'type': 'object'}}}, "
            "'schema_version': 1, 'target_sources': "
            "{'candidate_attack:CONTRACTS': source_digest}}"
        )
        + "CONTRACTS = {}\n",
        encoding="utf-8",
    )
    data = declaration_data()
    data["targets"]["contracts"] = "candidate_attack:CONTRACTS"

    declaration = write_semantic_inputs(tmp_path, data)
    result = CertificationRunner(
        tmp_path, declaration, check_python=Path(sys.executable)
    )._command("contracts")

    assert result.status == "passed", result.detail


def test_candidate_cannot_forge_result_capability_then_exit_zero(tmp_path: Path) -> None:
    source = tmp_path / "candidate_attack.py"
    source.write_text(
        "import array, hashlib, json, os, socket, tempfile\n"
        "source_digest = 'sha256:' + hashlib.sha256(open(__file__, 'rb').read()).hexdigest()\n"
        "payload = {'check': 'contracts', 'evidence': {'contracts': "
        "{'contract://payload': {'properties': {'value': {'title': 'Value', "
        "'type': 'string'}}, 'required': ['value'], 'title': 'Payload', "
        "'type': 'object'}}}, 'schema_version': 1, 'target_sources': "
        "{'candidate_attack:CONTRACTS': source_digest}}\n"
        "writable, path = tempfile.mkstemp()\n"
        "os.write(writable, json.dumps(payload).encode()); os.close(writable)\n"
        "readable = os.open(path, os.O_RDONLY); os.unlink(path)\n"
        "rights = array.array('i', [readable])\n"
        "channel = socket.socket(fileno=os.dup(1))\n"
        "channel.sendmsg([b'R'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, rights)])\n"
        "os._exit(0)\n"
        "CONTRACTS = {}\n",
        encoding="utf-8",
    )
    data = declaration_data()
    data["targets"]["contracts"] = "candidate_attack:CONTRACTS"

    result = CertificationRunner(
        tmp_path, AppDeclaration.model_validate(data), check_python=Path(sys.executable)
    )._command("contracts")

    assert result.status == "failed"
    assert "candidate" in result.detail.lower()


def test_candidate_cannot_forge_result_capability_and_complete_handshake(
    tmp_path: Path,
) -> None:
    source = tmp_path / "candidate_attack.py"
    source.write_text(
        "import array, hashlib, json, os, signal, socket, tempfile\n"
        "source_digest = 'sha256:' + hashlib.sha256(open(__file__, 'rb').read()).hexdigest()\n"
        "payload = {'check': 'contracts', 'evidence': {'contracts': "
        "{'contract://payload': {'properties': {'value': {'title': 'Value', "
        "'type': 'string'}}, 'required': ['value'], 'title': 'Payload', "
        "'type': 'object'}}}, 'schema_version': 1, 'target_sources': "
        "{'candidate_attack:CONTRACTS': source_digest}}\n"
        "writable, path = tempfile.mkstemp()\n"
        "os.write(writable, json.dumps(payload).encode()); os.close(writable)\n"
        "readable = os.open(path, os.O_RDONLY); os.unlink(path)\n"
        "rights = array.array('i', [readable])\n"
        "channel = socket.socket(fileno=os.dup(1))\n"
        "channel.sendmsg([b'R'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, rights)])\n"
        "challenge = channel.recv(33)\n"
        "channel.send(b'A' + challenge[1:])\n"
        "while True: signal.pause()\n"
        "CONTRACTS = {}\n",
        encoding="utf-8",
    )
    data = declaration_data()
    data["targets"]["contracts"] = "candidate_attack:CONTRACTS"

    result = CertificationRunner(
        tmp_path, AppDeclaration.model_validate(data), check_python=Path(sys.executable)
    )._command("contracts")

    assert result.status == "failed"
    assert "candidate" in result.detail.lower()


def test_candidate_source_policy_allows_guarded_cli_exit(tmp_path: Path) -> None:
    (tmp_path / "candidate.py").write_text(
        "from pydantic import BaseModel\n"
        "class Payload(BaseModel):\n    value: str\n"
        "CONTRACTS = {'contract://payload': Payload}\n"
        "if __name__ == '__main__':\n    raise SystemExit(1)\n",
        encoding="utf-8",
    )
    data = declaration_data()
    data["targets"]["contracts"] = "candidate:CONTRACTS"
    declaration = write_semantic_inputs(tmp_path, data)

    result = CertificationRunner(
        tmp_path, declaration, check_python=Path(sys.executable)
    )._command("contracts")

    assert result.status == "passed", result.detail


def test_optional_contract_does_not_offer_a_candidate_import_channel(tmp_path: Path) -> None:
    source = tmp_path / "candidate_attack.py"
    source.write_text(
        "import json, os, sys\n"
        "reference = sys.argv[sys.argv.index('--reference') + 1]\n"
        + _descriptor_forgery(
            "{'operation': 'import-target', 'reference': reference, 'schema_version': 1}"
        ),
        encoding="utf-8",
    )
    data = declaration_data()
    data["zeroth_version"] = importlib.metadata.version("zeroth-core")
    data["targets"].update(
        {
            "graph_builders": ["candidate_attack:build_graph"],
            "contracts": "candidate_attack:CONTRACTS",
            "auth_config": "candidate_attack:build_auth_config",
            "policy_guard": "candidate_attack:build_policy_guard",
            "migration_runner": "candidate_attack:migrate",
        }
    )

    declaration = write_semantic_inputs(tmp_path, data)
    result = CertificationRunner(
        tmp_path, declaration, check_python=Path(sys.executable)
    )._command("optional-extras")

    assert result.status == "passed", result.detail


def test_descriptor_scan_cannot_forge_reducer_resolution(tmp_path: Path) -> None:
    (tmp_path / "candidate.py").write_text(
        "from pydantic import BaseModel\n"
        "from zeroth.contracts.graph import AgentNode, AgentNodeData, Graph\n"
        "from zeroth.runtime.parallel.models import ParallelConfig\n"
        "class Payload(BaseModel):\n    value: str\n"
        "CONTRACTS = {'contract://payload': Payload}\n"
        "def build_graph():\n"
        "    node = AgentNode(node_id='start', graph_version_ref='app@1', "
        "input_contract_ref='contract://payload', output_contract_ref='contract://payload', "
        "agent=AgentNodeData(instruction='go', model_provider='test'), "
        "parallel_config=ParallelConfig(split_path='items', merge_strategy='custom', "
        "reducer_ref='reducer_attack.merge'))\n"
        "    return Graph(graph_id='app', name='App', entry_step='start', "
        "nodes=[node], edges=[])\n",
        encoding="utf-8",
    )
    (tmp_path / "reducer_attack.py").write_text(
        "import json, os, sys\n"
        "if sys.argv[1] == 'resolve-reducer':\n"
        "    reference = sys.argv[sys.argv.index('--reference') + 1]\n"
        + "    "
        + _descriptor_forgery(
            "{'operation': 'resolve-reducer', 'reference': reference, 'schema_version': 1}"
        )
        .replace("\n", "\n    ")
        .rstrip()
        + "\n"
        "def merge(left, right):\n    return right\n",
        encoding="utf-8",
    )
    data = declaration_data()
    data["targets"]["graph_builders"] = ["candidate:build_graph"]
    data["targets"]["contracts"] = "candidate:CONTRACTS"
    declaration = write_semantic_inputs(
        tmp_path, data, updates={"reducers": ["reducer_attack.merge"]}
    )

    result = CertificationRunner(
        tmp_path, declaration, check_python=Path(sys.executable)
    )._command("graph")

    assert result.status == "failed"
    assert "reducer" in result.detail.lower()


def _git(repo: Path, *args: str, output: Path | None = None) -> str:
    command = ["git", "-C", str(repo), *args]
    if output is not None:
        with output.open("wb") as stream:
            subprocess.run(command, check=True, stdout=stream, stderr=subprocess.PIPE)
        return ""
    completed = subprocess.run(command, check=True, capture_output=True, text=True)
    return completed.stdout.strip() if output is None else ""


def _commit(repo: Path, content: str, message: str) -> str:
    (repo / "app.txt").write_text(content, encoding="utf-8")
    _git(repo, "add", "app.txt")
    _git(
        repo,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        message,
    )
    return _git(repo, "rev-parse", "HEAD")


def test_source_archive_content_must_match_external_commit_tree(tmp_path: Path) -> None:
    repo = tmp_path / "app"
    repo.mkdir()
    _git(repo, "init", "-q")
    expected_commit = _commit(repo, "expected\n", "expected")
    forged_commit = _commit(repo, "forged\n", "forged")
    forged_archive = tmp_path / "forged.tar"
    _git(repo, "archive", "--format=tar", forged_commit, output=forged_archive)
    _git(repo, "checkout", "--detach", "-q", expected_commit)
    candidate = identity().model_copy(
        update={
            "app_commit": expected_commit,
            "source_digest": "sha256:" + hashlib.sha256(forged_archive.read_bytes()).hexdigest(),
        }
    )

    with pytest.raises(ValueError, match="source archive.*tree|source archive.*content"):
        validate_source_archive(forged_archive, candidate, repository=repo)


def test_dependency_hooks_use_bounded_container_and_always_cleanup() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["certify"]
    prepare = next(step["run"] for step in job["steps"] if step.get("id") == "prepare")
    cleanup = next(
        step for step in job["steps"] if step["name"] == "Clean up certification resources"
    )

    assert "run-dependency-sandbox" in prepare
    assert "timeout 10m sudo -H -u app-cert-candidate" not in prepare
    for boundary in ("--cpus", "--memory", "--pids-limit", "--disk-bytes", "--timeout"):
        assert boundary in prepare
    assert cleanup["if"] == "${{ always() }}"
    assert "cleanup-certification" in cleanup["run"]


def test_dependency_sandbox_kills_detached_descendant_and_caps_output(tmp_path: Path) -> None:
    sandbox = importlib.import_module("release.app_certification.dependency_sandbox")
    docker = tmp_path / "docker"
    pid_file = tmp_path / "child.pid"
    marker = tmp_path / "descendant-survived"
    gate = tmp_path / "release-descendant"
    cleanup = tmp_path / "cleanup"
    child_code = (
        "import pathlib,time; "
        f"gate=pathlib.Path({str(gate)!r}); "
        "\nwhile not gate.exists(): time.sleep(.01)\n"
        f"pathlib.Path({str(marker)!r}).write_text('alive'); time.sleep(30)"
    )
    docker.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, signal, subprocess, sys, time\n"
        f"pid_file=pathlib.Path({str(pid_file)!r}); marker=pathlib.Path({str(marker)!r}); cleanup=pathlib.Path({str(cleanup)!r})\n"
        "if sys.argv[1] == 'run':\n"
        f"    child=subprocess.Popen([sys.executable,'-c',{child_code!r}], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "    pid_file.write_text(str(child.pid)); os.write(1, b'x' * 16384); time.sleep(30)\n"
        "elif sys.argv[1:3] == ['rm', '-f']:\n"
        "    cleanup.write_text('called')\n"
        "    if pid_file.exists():\n"
        "        try: os.kill(int(pid_file.read_text()), signal.SIGKILL)\n"
        "        except ProcessLookupError: pass\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    log = tmp_path / "sandbox.log"

    with pytest.raises(RuntimeError, match="timed out"):
        sandbox.run_bounded_container(
            [str(docker), "run", "fixture"],
            docker=str(docker),
            container_name="fixture",
            log_path=log,
            timeout=1.0,
            output_limit=4096,
        )

    gate.write_text("release", encoding="utf-8")
    time.sleep(0.2)
    assert cleanup.read_text(encoding="utf-8") == "called"
    assert log.stat().st_size <= 4096
    assert not marker.exists()
