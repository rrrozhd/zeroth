from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from release.app_certification import candidate_supervisor
from release.app_certification import cli as certification_cli

ROOT = Path(__file__).parents[2]


def _boundaries(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "candidate"
    report = tmp_path / "report" / "report.json"
    evidence = tmp_path / "evidence"
    for path in (root, report.parent, evidence):
        path.mkdir(mode=0o700)
        path.chmod(0o700)
    return root, report, evidence


def _account(monkeypatch: pytest.MonkeyPatch, group_name: str = "app-cert-candidate"):
    account = SimpleNamespace(
        pw_dir="/home/app-cert-candidate",
        pw_gid=os.getegid() + 10_000,
        pw_name="app-cert-candidate",
        pw_shell="/usr/sbin/nologin",
        pw_uid=os.geteuid() + 10_000,
    )
    monkeypatch.setattr(certification_cli.pwd, "getpwnam", lambda _user: account)
    monkeypatch.setattr(
        certification_cli.os,
        "getgrouplist",
        lambda _user, _primary_gid: [account.pw_gid],
    )
    monkeypatch.setattr(
        certification_cli.grp,
        "getgrgid",
        lambda _gid: SimpleNamespace(gr_name=group_name, gr_mem=[]),
    )
    return account


def test_direct_run_rejects_a_username_nopasswd_sudo_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, report, evidence = _boundaries(tmp_path)
    account = _account(monkeypatch)

    def run(argv: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        if argv == ["pgrep", "-u", str(account.pw_uid)]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")
        if "passwd" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=f"{account.pw_name} L\n", stderr="")
        if argv[-3:] == ["sudo", "--non-interactive", "--list"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=f"User {account.pw_name} may run (root) NOPASSWD: ALL\n",
                stderr="",
            )
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")

    monkeypatch.setattr(certification_cli.subprocess, "run", run)

    with pytest.raises(ValueError, match="sudo|privileg"):
        certification_cli._validate_direct_run_isolation(account.pw_name, root, report, evidence)


def test_direct_run_rejects_a_custom_privileged_primary_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, report, evidence = _boundaries(tmp_path)
    account = _account(monkeypatch, group_name="disk")
    monkeypatch.setattr(
        certification_cli.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 1, stdout="", stderr=""),
    )

    with pytest.raises(ValueError, match="primary group|dedicated|privileg"):
        certification_cli._validate_direct_run_isolation(account.pw_name, root, report, evidence)


def test_named_acl_write_access_rejects_a_mode_0700_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, report, evidence = _boundaries(tmp_path)
    account = _account(monkeypatch)
    access_calls: list[list[str]] = []

    def run(argv: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        if "/usr/bin/test" in argv:
            access_calls.append(argv)
            granted = argv[-2:] == ["-w", str(root.resolve())]
            return subprocess.CompletedProcess(argv, 0 if granted else 1, stdout="", stderr="")
        if argv == ["pgrep", "-u", str(account.pw_uid)]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")
        if "passwd" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=f"{account.pw_name} L\n", stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")

    monkeypatch.setattr(certification_cli.subprocess, "run", run)

    with pytest.raises(ValueError, match="candidate root.*writable"):
        certification_cli._validate_direct_run_isolation(account.pw_name, root, report, evidence)

    assert access_calls


def test_uid_inventory_is_only_a_leak_signal_and_never_a_kill_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[0] != "pgrep":
            raise AssertionError("UID inventory must never authorize a kill")
        return subprocess.CompletedProcess(argv, 0, stdout="4242\n", stderr="")

    monkeypatch.setattr(candidate_supervisor.subprocess, "run", run)

    with pytest.raises(RuntimeError, match="survived|leak"):
        candidate_supervisor._terminate_candidate_user("app-cert-candidate")

    assert calls == [["pgrep", "-u", "app-cert-candidate"]]


def test_run_owned_cleanup_uses_stable_pidfds_during_pid_turnover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    children = iter(((101,), ()))
    opened: list[int] = []
    signaled: list[tuple[int, signal.Signals]] = []
    closed: list[int] = []

    monkeypatch.setattr(candidate_supervisor, "_direct_child_pids", lambda: next(children))
    monkeypatch.setattr(
        candidate_supervisor.os,
        "pidfd_open",
        lambda pid: opened.append(pid) or 9001,
        raising=False,
    )
    monkeypatch.setattr(
        candidate_supervisor.signal,
        "pidfd_send_signal",
        lambda fd, sig: signaled.append((fd, sig)),
        raising=False,
    )
    monkeypatch.setattr(candidate_supervisor.os, "close", closed.append)
    monkeypatch.setattr(candidate_supervisor.os, "waitpid", lambda _pid, _flags: (101, 0))
    monkeypatch.setattr(
        candidate_supervisor.os,
        "kill",
        lambda _pid, _sig: (_ for _ in ()).throw(AssertionError("numeric PID kill is unsafe")),
    )

    candidate_supervisor._terminate_run_children()

    assert opened == [101]
    assert signaled == [(9001, signal.SIGKILL)]
    assert closed == [9001]


def test_candidate_prefix_blocks_privilege_gains_and_drops_capabilities() -> None:
    prefix = candidate_supervisor._probe_prefix("app-cert-candidate")

    assert "/usr/bin/setpriv" in prefix
    assert "--no-new-privs" in prefix
    assert "--bounding-set=-all" in prefix
    assert "--ambient-caps=-all" in prefix
    assert "--inh-caps=-all" in prefix


def test_workflow_creates_a_fresh_locked_private_candidate_account() -> None:
    workflow = (ROOT / ".github/workflows/app-certification.yml").read_text(encoding="utf-8")
    useradd = next(line for line in workflow.splitlines() if "useradd" in line)

    assert "--system" in useradd
    assert "--user-group" in useradd
    assert "--password '!'" in useradd


def test_untrusted_probe_runs_inside_a_root_owned_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], str | None]] = []

    def run(argv: list[str], *, candidate_user: str | None = None, timeout_seconds: int = 150):
        assert timeout_seconds == 180
        calls.append((argv, candidate_user))
        return 0, "", ""

    monkeypatch.setattr(candidate_supervisor, "run_importer", run)

    candidate_supervisor.probe_candidate(
        "run-migration",
        tmp_path,
        tmp_path / ".venv",
        reference="candidate:run",
        database_url="sqlite:///candidate.sqlite",
        untrusted_user="app-cert-candidate",
    )

    argv, candidate_user = calls[0]
    assert argv[:3] == ["sudo", "--non-interactive", "--"]
    assert candidate_user is None
    assert candidate_supervisor._ROOT_BOUNDARY_BOOTSTRAP in argv
