from __future__ import annotations

import grp
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from release.app_certification import CertificationRunner
from release.app_certification import candidate_supervisor
from release.app_certification import cli as certification_cli
from tests.app_certification.test_engine import (
    COMMIT,
    DIGEST,
    SOURCE_DIGEST,
    declaration_data,
    passing_executor,
    passing_http,
    write_inputs,
    write_semantic_inputs,
)


def _protected_boundaries(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "candidate-root"
    report_directory = tmp_path / "report-boundary"
    evidence = tmp_path / "evidence-boundary"
    for path in (root, report_directory, evidence):
        path.mkdir(mode=0o700)
        path.chmod(0o700)
    return root, report_directory / "report.json", evidence


def _install_candidate_account(
    monkeypatch: pytest.MonkeyPatch,
    *,
    groups: list[int] | None = None,
    processes: bool = False,
) -> SimpleNamespace:
    account = SimpleNamespace(
        pw_dir="/home/app-cert-candidate",
        pw_gid=os.getegid() + 10_000,
        pw_name="app-cert-candidate",
        pw_shell="/usr/sbin/nologin",
        pw_uid=os.geteuid() + 10_000,
    )
    candidate_groups = groups or [account.pw_gid]
    monkeypatch.setattr(certification_cli.pwd, "getpwnam", lambda _user: account)
    monkeypatch.setattr(
        certification_cli.os,
        "getgrouplist",
        lambda _user, _primary_gid: candidate_groups,
    )
    monkeypatch.setattr(
        grp,
        "getgrgid",
        lambda _gid: SimpleNamespace(gr_name=account.pw_name),
    )

    def inventory(argv: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        assert argv == ["pgrep", "-u", str(account.pw_uid)]
        return subprocess.CompletedProcess(
            argv,
            0 if processes else 1,
            stdout="4242\n" if processes else "",
            stderr="",
        )

    monkeypatch.setattr(certification_cli.subprocess, "run", inventory)
    return account


def test_candidate_cannot_mix_a_replaced_declaration_into_later_checks(
    tmp_path: Path,
) -> None:
    write_inputs(tmp_path)
    data = declaration_data()
    declaration = write_semantic_inputs(tmp_path, data)
    declaration_path = tmp_path / "certification.json"
    mutated = json.loads(json.dumps(data))
    mutated["targets"]["frontend_path"] = "candidate-selected-frontend"
    observed_frontends: list[str] = []

    def mutate_after_migration(argv: list[str], cwd: Path):
        result = passing_executor(argv, cwd)
        if "--declaration-json" in argv:
            declaration_path.write_text(json.dumps(mutated), encoding="utf-8")
        elif "--declaration" in argv:
            path = Path(argv[argv.index("--declaration") + 1])
            observed_frontends.append(
                json.loads(path.read_text(encoding="utf-8"))["targets"]["frontend_path"]
            )
        return result

    report = CertificationRunner(
        tmp_path,
        declaration,
        executor=mutate_after_migration,
        http=passing_http,
        commit_reader=lambda _root: COMMIT,
        declaration_path=declaration_path,
    ).run(
        expected_commit=COMMIT,
        image_digest=DIGEST,
        source_digest=SOURCE_DIGEST,
    )

    assert observed_frontends
    assert set(observed_frontends) == {"frontend"}
    assert report.status == "failed"
    assert any(
        "declaration" in check.detail.lower()
        and any(word in check.detail.lower() for word in ("changed", "identity", "replaced"))
        for check in report.checks
    )


def test_candidate_root_replacement_is_detected_even_with_identical_bytes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "candidate-root"
    root.mkdir()
    write_inputs(root)
    declaration = write_semantic_inputs(root, declaration_data())
    original = tmp_path / "original-root"
    replaced = False

    def replace_after_migration(argv: list[str], cwd: Path):
        nonlocal replaced
        result = passing_executor(argv, cwd)
        if "--declaration-json" in argv and not replaced:
            root.rename(original)
            shutil.copytree(original, root)
            replaced = True
        return result

    report = CertificationRunner(
        root,
        declaration,
        executor=replace_after_migration,
        http=passing_http,
        commit_reader=lambda _root: COMMIT,
    ).run(
        expected_commit=COMMIT,
        image_digest=DIGEST,
        source_digest=SOURCE_DIGEST,
    )

    assert replaced
    assert report.status == "failed"
    assert any(
        "root" in check.detail.lower()
        and any(word in check.detail.lower() for word in ("changed", "identity", "replaced"))
        for check in report.checks
    )


def test_direct_run_rejects_a_candidate_replaceable_root_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    replaceable = tmp_path / "candidate-writable-parent"
    replaceable.mkdir(mode=0o777)
    replaceable.chmod(0o777)
    root = replaceable / "candidate-root"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    report_directory = tmp_path / "report-boundary"
    evidence = tmp_path / "evidence-boundary"
    for path in (report_directory, evidence):
        path.mkdir(mode=0o700)
        path.chmod(0o700)
    _install_candidate_account(monkeypatch)

    with pytest.raises(ValueError, match="ancestor|replace"):
        certification_cli._validate_direct_run_isolation(
            "app-cert-candidate",
            root,
            report_directory / "report.json",
            evidence,
        )


def test_direct_run_rejects_an_active_candidate_before_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, report, evidence = _protected_boundaries(tmp_path)
    _install_candidate_account(monkeypatch, processes=True)
    cleanup_calls: list[str] = []
    monkeypatch.setattr(
        candidate_supervisor,
        "_terminate_candidate_user",
        lambda user: cleanup_calls.append(user),
    )

    with pytest.raises(ValueError, match="active|process|dedicated"):
        certification_cli._validate_direct_run_isolation(
            "app-cert-candidate", root, report, evidence
        )

    assert cleanup_calls == []


def test_direct_run_rejects_a_privileged_candidate_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, report, evidence = _protected_boundaries(tmp_path)
    privileged_gid = os.getegid() + 20_000
    account = _install_candidate_account(
        monkeypatch,
        groups=[os.getegid() + 10_000, privileged_gid],
    )
    monkeypatch.setattr(
        grp,
        "getgrgid",
        lambda gid: SimpleNamespace(gr_name="docker" if gid == privileged_gid else account.pw_name),
    )

    with pytest.raises(ValueError, match="privileged|docker"):
        certification_cli._validate_direct_run_isolation(
            "app-cert-candidate", root, report, evidence
        )


def test_direct_run_rejects_a_lookalike_login_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, report, evidence = _protected_boundaries(tmp_path)
    account = _install_candidate_account(monkeypatch)
    account.pw_shell = "/home/app-cert-candidate/nologin"

    with pytest.raises(ValueError, match="disabled login shell"):
        certification_cli._validate_direct_run_isolation(
            "app-cert-candidate", root, report, evidence
        )


def test_candidate_cleanup_avoids_account_wide_kill_without_survivors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def inventory_only(argv: list[str], **_kwargs) -> subprocess.CompletedProcess[bytes]:
        calls.append(argv)
        if argv[0] != "pgrep":
            raise AssertionError("account-wide kill must not run without surviving processes")
        return subprocess.CompletedProcess(argv, 1)

    monkeypatch.setattr(candidate_supervisor.subprocess, "run", inventory_only)

    candidate_supervisor._terminate_candidate_user("app-cert-candidate")

    assert calls == [["pgrep", "-u", "app-cert-candidate"]]


def test_candidate_cleanup_verifies_the_final_targeted_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventories = iter(
        (
            subprocess.CompletedProcess([], 0, stdout="101\n"),
            subprocess.CompletedProcess([], 0, stdout="102\n"),
            subprocess.CompletedProcess([], 0, stdout="103\n"),
            subprocess.CompletedProcess([], 1, stdout=""),
        )
    )
    killed: list[str] = []

    def run(argv: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        if argv[0] == "pgrep":
            return next(inventories)
        killed.append(argv[-1])
        return subprocess.CompletedProcess(argv, 0, stdout="")

    monkeypatch.setattr(candidate_supervisor.subprocess, "run", run)

    candidate_supervisor._terminate_candidate_user("app-cert-candidate")

    assert killed == ["101", "102", "103"]
