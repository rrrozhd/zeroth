from __future__ import annotations

import json
import os
import pwd
import signal
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from release.app_certification import checks
from release.app_certification import cli as certification_cli
from release.app_certification import candidate_supervisor
from release.app_certification import migration_supervisor
from tests.app_certification.test_engine import declaration_data, write_semantic_inputs


ROOT = Path(__file__).parents[2]
WORKFLOW = ROOT / ".github/workflows/app-certification.yml"


def _certify_step(step_id: str) -> dict:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return next(step for step in workflow["jobs"]["certify"]["steps"] if step.get("id") == step_id)


@pytest.mark.parametrize(
    "tenant",
    [
        "$(touch /tmp/app-certification-command-substitution)",
        "tenant-acme\nimage_reference=attacker/overwrite:latest",
    ],
    ids=["command-substitution", "github-output-collision"],
)
def test_runtime_settings_reject_hostile_candidate_tenant(tmp_path: Path, tenant: str) -> None:
    app = tmp_path / "app"
    app.mkdir()
    shutil.copytree(ROOT / "apps/vendor_dd", app / "apps/vendor_dd")
    declaration = write_semantic_inputs(app, declaration_data())
    semantic_path = app / declaration.semantic_path
    semantic = json.loads(semantic_path.read_text(encoding="utf-8"))
    for credential in semantic["service_config"]["auth_config"]["api_keys"]:
        credential["tenant_id"] = tenant
    semantic_path.write_text(json.dumps(semantic), encoding="utf-8")
    validate = getattr(checks, "validated_runtime_settings", None)

    assert callable(validate), "certifier has no authoritative runtime-settings validator"
    with pytest.raises(ValueError, match="tenant"):
        validate(app, declaration)


def test_workflow_never_promotes_candidate_tenant_or_tag_into_trusted_shell() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["certify"]
    scripts = "\n".join(step.get("run", "") for step in job["steps"])
    prepare = _certify_step("prepare")["run"]
    containers = _certify_step("containers")

    assert "validate-runtime-settings" in prepare
    assert prepare.index("validate-runtime-settings") < prepare.index("runtime_tenant=")
    assert "${{ steps.prepare.outputs.runtime_tenant }}" not in scripts
    assert "steps.prepare.outputs.image_reference" not in WORKFLOW.read_text(encoding="utf-8")
    assert 'echo "image_reference=$RUNTIME_IMAGE_REFERENCE"' not in prepare
    assert containers["env"]["RUNTIME_TENANT"] == "${{ steps.prepare.outputs.runtime_tenant }}"
    assert '--env ECP_SERVICE_PRINCIPAL_TENANT_ID="$RUNTIME_TENANT"' in containers["run"]
    assert '--tag "$RUNTIME_IMAGE_REFERENCE"' in _certify_step("image")["run"]


def _direct_run_args(tmp_path: Path, user: str | None) -> Namespace:
    return Namespace(
        app_commit="a" * 40,
        check_python=None,
        declaration=tmp_path / "certification.json",
        ephemeral_url="http://127.0.0.1:18081",
        evidence_root=tmp_path / "evidence",
        image_digest="sha256:" + "b" * 64,
        image_reference="app-cert-runtime:fixture",
        packaged_url="http://127.0.0.1:18080",
        report=tmp_path / "report.json",
        root=tmp_path,
        source_digest="sha256:" + "c" * 64,
        untrusted_user=user,
    )


def _install_fake_run_boundary(monkeypatch: pytest.MonkeyPatch, marker: Path) -> None:
    class FakeRunner:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def run(self, **_kwargs):
            code = (
                "import pathlib,time; time.sleep(0.2); "
                f"pathlib.Path({str(marker)!r}).write_text('tampered')"
            )
            subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
            return SimpleNamespace(status="passed")

    monkeypatch.setattr(
        certification_cli, "load_declaration", lambda _path: SimpleNamespace(smoke=None)
    )
    monkeypatch.setattr(certification_cli, "resolve_smoke_headers", lambda _smoke: {})
    monkeypatch.setattr(certification_cli, "CertificationRunner", FakeRunner)
    monkeypatch.setattr(
        certification_cli, "write_report", lambda _report, path: path.write_text("ok")
    )


def test_direct_run_without_isolation_stops_before_detached_child_can_tamper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "post-write-tamper"
    _install_fake_run_boundary(monkeypatch, marker)

    with pytest.raises(ValueError, match="isolation|untrusted"):
        certification_cli._run(_direct_run_args(tmp_path, None))

    time.sleep(0.3)
    assert not marker.exists()


def test_direct_run_rejects_the_certifier_os_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "same-user-ran"
    _install_fake_run_boundary(monkeypatch, marker)
    current_user = pwd.getpwuid(os.geteuid()).pw_name

    with pytest.raises(ValueError, match="distinct|same|certifier"):
        certification_cli._run(_direct_run_args(tmp_path, current_user))

    time.sleep(0.3)
    assert not marker.exists()


def test_direct_run_rejects_candidate_writable_evidence_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    validate = getattr(certification_cli, "_validate_direct_run_isolation", None)
    assert callable(validate), "direct-run isolation boundary is missing"
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o777)
    evidence.chmod(0o777)
    fake_user = SimpleNamespace(pw_gid=os.getegid() + 1000, pw_uid=os.geteuid() + 1000)
    monkeypatch.setattr(certification_cli.pwd, "getpwnam", lambda _user: fake_user)
    monkeypatch.setattr(certification_cli.os, "getgrouplist", lambda _user, gid: [gid])
    monkeypatch.setattr(certification_cli, "_validate_candidate_account", lambda *_args: None)
    monkeypatch.setattr(
        certification_cli,
        "_candidate_access",
        lambda path, _user, mode: mode == "-w" and bool(path.stat().st_mode & stat.S_IWOTH),
    )

    with pytest.raises(ValueError, match="evidence.*writable"):
        validate("isolated-candidate", tmp_path, tmp_path / "report.json", evidence)


def test_candidate_supervisor_sweeps_a_detached_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "detached-tamper"
    pid_file = tmp_path / "detached.pid"
    child = f"import pathlib,time; time.sleep(.4); pathlib.Path({str(marker)!r}).write_text('x')"
    parent = (
        "import pathlib,subprocess,sys; "
        f"p=subprocess.Popen([sys.executable,'-c',{child!r}],start_new_session=True); "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid))"
    )
    owned_cleanup: list[int] = []
    verified: list[str] = []

    def cleanup() -> None:
        pid = int(pid_file.read_text(encoding="utf-8"))
        owned_cleanup.append(pid)
        os.kill(pid, signal.SIGKILL)

    monkeypatch.setattr(candidate_supervisor, "_enable_subreaper", lambda: None)
    monkeypatch.setattr(candidate_supervisor, "_terminate_run_children", cleanup)
    monkeypatch.setattr(
        candidate_supervisor,
        "_terminate_candidate_user",
        lambda user: verified.append(user),
    )
    returncode, _, _ = candidate_supervisor._wait_process(
        [sys.executable, "-c", parent],
        stdout=subprocess.DEVNULL,
        candidate_user="isolated-candidate",
    )

    time.sleep(0.5)
    assert returncode == 0
    assert len(owned_cleanup) == 1
    assert verified == ["isolated-candidate"]
    assert not marker.exists()


def test_sqlite_migration_opens_only_its_supervised_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    declaration = write_semantic_inputs(tmp_path, declaration_data())
    inspected_modes: list[int] = []
    inspect = migration_supervisor._sqlite_tables

    def run_candidate(_reference: str, database_url: str) -> None:
        database = Path(database_url.removeprefix("sqlite:///"))
        assert stat.S_IMODE(database.parent.stat().st_mode) == 0o733
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE candidate_migration (id INTEGER PRIMARY KEY)")

    def inspect_protected(database: Path) -> list[str]:
        inspected_modes.append(stat.S_IMODE(database.parent.stat().st_mode))
        return inspect(database)

    monkeypatch.setattr(migration_supervisor, "_sqlite_tables", inspect_protected)
    evidence = migration_supervisor.inspect_migration(declaration, run_candidate, backend="sqlite")

    assert evidence["object_count"] == 1
    assert inspected_modes == [0o700]


def test_sqlite_migration_rejects_candidate_symlink(tmp_path: Path) -> None:
    declaration = write_semantic_inputs(tmp_path, declaration_data())
    outside = tmp_path / "outside.sqlite"
    with sqlite3.connect(outside) as connection:
        connection.execute("CREATE TABLE outside_database (id INTEGER PRIMARY KEY)")

    def run_candidate(_reference: str, database_url: str) -> None:
        Path(database_url.removeprefix("sqlite:///")).symlink_to(outside)

    with pytest.raises(ValueError, match="symlink"):
        migration_supervisor.inspect_migration(declaration, run_candidate, backend="sqlite")
